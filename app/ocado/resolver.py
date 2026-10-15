"""From what someone said to a change to the coming Ocado order.

"Add some protein bread, but not the usual one, the other one. It's got like that
green packaging" can't be cut down to an item name: "not the usual one" is the
point of it. Noodle sends the words as they were said; a model with read-only
tools works out which products they mean, and answers with a *plan*. Nothing is
written to Ocado here: :func:`apply` does that, later and once, with whatever the
plan has become by then (Noodle waits a few minutes for "actually, two").

The tools read the household's own buying first, since that's what messy
references point at: the usual loaf is the one on thirteen orders, "the other
one" the loaf on three, and the packaging description confirms it. A live Ocado
search is the last resort, two per request and throttled across requests,
because a burst of searches gets the connection bot-checked.

The model proposes; the code decides what's allowed. Whatever the model says, a
plan may only:

* name products a tool showed it (no invented SKUs);
* remove, set or swap out a line that's on the order or in the pending plan, and
  that the words mentioned (a word of the product's name, or its ingredient's,
  is in what was said);
* change at most :data:`MAX_LINES` lines, with quantities 1 to :data:`MAX_QUANTITY`.

Anything it breaks makes the plan *unsure*: nothing changes and the person is
asked. Every change in a plan comes back with the product's name, so the reply
can list all of them.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.db.models import IngredientMapping, IngredientMappingProduct, Product, Purchase, RetailerAccount
from app.ocado import amend, purchases, top_up
from app.ocado.client import OcadoClient
from app.planner.preferences import pack_preferences

log = logging.getLogger(__name__)

RETAILER = "ocado"
MAX_LINES = 8
MAX_QUANTITY = top_up.MAX_QUANTITY
#: Live searches one request may make.
SEARCHES_PER_REQUEST = 2
#: Live searches across all requests in :data:`SEARCH_WINDOW` seconds.
SEARCHES_PER_WINDOW = 4
SEARCH_WINDOW = 15 * 60
#: Model turns before giving up on an answer.
MAX_TURNS = 8
#: Rows a history lookup returns.
HISTORY_ROWS = 25
OPS = ("add", "remove", "set", "swap")
CONFIDENCE = ("sure", "likely", "unsure")
#: Words that don't say which product: "take the bread off" mentions bread, not "the".
FILLER = top_up.FILLER | {"x", "g", "kg", "ml", "l", "pack", "ocado", "s", "on", "off", "to", "for", "it", "one"}

_searches: deque[float] = deque()
_search_lock = threading.Lock()


class ResolveError(RuntimeError):
    """The model couldn't produce a plan (down, or never settled on an answer)."""


# --- what the tools see ------------------------------------------------------


@dataclass
class Order:
    account: RetailerAccount
    client: OcadoClient
    raw: dict[str, Any]
    lines: dict[str, int]

    @property
    def order_id(self) -> str:
        return str(self.raw["orderId"])

    def about(self) -> dict[str, Any]:
        dates = self.raw.get("dates") or {}
        return {"order_id": self.order_id, "account": self.account.email,
                "delivery_start": dates.get("deliveryStartDate"), "delivery_end": dates.get("deliveryEndDate"),
                "edit_until": dates.get("confirmOrderChangesBy") or dates.get("deliveryLatestUpdateTime")}


@dataclass
class Context:
    db: Session
    user_id: int
    accounts: list[tuple[RetailerAccount, OcadoClient]]
    #: The plan so far (changes not yet applied), as ``{"op", "sku", "quantity", "replaces"}``.
    pending: list[dict[str, Any]] = field(default_factory=list)
    today: date = field(default_factory=date.today)
    searches_left: int = SEARCHES_PER_REQUEST
    #: Every product a tool has shown the model: the only SKUs a plan may name.
    seen: dict[str, str] = field(default_factory=dict)
    #: The model sees short ids ("p3"), not SKUs: copying a 36-character UUID is where it slips.
    refs: dict[str, str] = field(default_factory=dict)
    _order: Order | None = None

    def ref(self, sku: str) -> str:
        if sku not in self.refs:
            self.refs[sku] = f"p{len(self.refs) + 1}"
        return self.refs[sku]

    def sku(self, ref: str | None) -> str | None:
        """The SKU behind a short id (a SKU given as itself passes through)."""
        if ref is None:
            return None
        return next((sku for sku, r in self.refs.items() if r == ref), ref)

    def shown(self, changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Changes as the model sees them: short ids."""
        return [{**c, "sku": self.ref(c["sku"]), "replaces": self.ref(c["replaces"]) if c.get("replaces") else None}
                for c in changes]

    def unshown(self, changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**c, "sku": self.sku(c.get("sku")), "replaces": self.sku(c.get("replaces"))} for c in changes]

    @property
    def household(self) -> list[int]:
        return top_up.household_ids(self.accounts)

    def order(self) -> Order:
        if self._order is None:
            account, client, raw = top_up.next_order(self.accounts)
            self._order = Order(account, client, raw, top_up.order_quantities(client, str(raw["orderId"])))
            self.names(list(self._order.lines))
        return self._order

    def names(self, skus: list[str]) -> dict[str, str]:
        """Names for ``skus`` from the catalogue, remembered as seen."""
        found = dict(self.db.execute(select(Product.sku, Product.name)
                                     .where(Product.retailer == RETAILER, Product.sku.in_(skus))).all())
        for sku in skus:
            self.seen[sku] = found.get(sku) or self.seen.get(sku) or sku
        return {sku: self.seen[sku] for sku in skus}


def _product_rows(ctx: Context, skus: list[str]) -> list[dict[str, Any]]:
    """What the model is told about each product: name, pack, price, how often bought, what it looks like."""
    products = {p.sku: p for p in ctx.db.scalars(select(Product).where(Product.retailer == RETAILER,
                                                                         Product.sku.in_(skus)))}
    bought = purchases.bought(ctx.db, ctx.household, skus)
    looks = purchases.descriptions(ctx.db, skus)
    rows = []
    for sku in skus:
        p, b = products.get(sku), bought.get(sku)
        if p is not None:
            ctx.seen[sku] = p.name
        rows.append({k: v for k, v in {
            "id": ctx.ref(sku), "name": ctx.seen.get(sku, sku), "brand": p.brand if p else None,
            "pack": p.pack_size_raw if p else None, "price": p.price if p else None,
            "in_stock": None if p is None or p.in_stock is None else bool(p.in_stock),
            "orders": b.orders if b else 0, "bought_quantity": b.quantity if b else 0,
            "first_bought": b.first if b else None, "last_bought": b.last if b else None,
            "packaging": looks.get(sku),
        }.items() if v is not None})
    return rows


# --- the tools ---------------------------------------------------------------


def tool_history(ctx: Context, query: str | None = None, since: str | None = None) -> dict[str, Any]:
    """Products bought, most often first, whose name, brand or category share a word with ``query``."""
    rows = ctx.db.execute(
        select(Purchase.sku, Product.name, Product.brand, Product.category)
        .outerjoin(Product, (Product.sku == Purchase.sku) & (Product.retailer == RETAILER))
        .where(Purchase.retailer == RETAILER, Purchase.user_id.in_(ctx.household),
               *([Purchase.delivered_on >= since] if since else []))
        .distinct()
    ).all()
    wanted = purchases.words(query or "") - FILLER
    scores: dict[str, int] = {}
    for sku, name, brand, category in rows:
        have = purchases.words(" ".join(x for x in (name, brand, category) if x))
        score = len(wanted & have) if wanted else 1
        if score:
            scores[sku] = max(score, scores.get(sku, 0))
    bought = purchases.bought(ctx.db, ctx.household, list(scores))
    ranked = sorted(scores, key=lambda sku: (-scores[sku], -bought[sku].orders, sku))[:HISTORY_ROWS]
    return {"orders_in_history": purchases.order_count(ctx.db, ctx.household),
            "products": _product_rows(ctx, ranked)}


def tool_mapping(ctx: Context, name: str) -> dict[str, Any]:
    """The products accepted for an ingredient ("oat milk"), in the order they'd be bought."""
    found = top_up.match(ctx.db, name)
    if isinstance(found, list):
        return {"matched": None, "could_be": found}
    mapping = ctx.db.scalar(select(IngredientMapping).where(IngredientMapping.retailer == RETAILER,
                                                            IngredientMapping.ingredient_key == found.key))
    skus = list(ctx.db.scalars(select(IngredientMappingProduct.sku)
                               .where(IngredientMappingProduct.mapping_id == mapping.id,
                                      IngredientMappingProduct.accepted == 1)
                               .order_by(IngredientMappingProduct.rank)))
    product, _ = top_up.pick(ctx.db, found, ctx.user_id, household=ctx.household)
    return {"matched": found.name, "default": product.sku if product else None,
            "pack_choice": pack_preferences(ctx.db, ctx.user_id, retailer=RETAILER).get(found.key),
            "products": _product_rows(ctx, skus)}


def tool_order(ctx: Context) -> dict[str, Any]:
    order = ctx.order()
    return {**order.about(),
            "lines": [{**row, "quantity": quantity}
                      for row, quantity in zip(_product_rows(ctx, list(order.lines)), order.lines.values())],
            "pending_changes": [{**c, "name": ctx.seen.get(c["sku"], c["sku"])} for c in ctx.shown(ctx.pending)]}


def tool_product(ctx: Context, id: str) -> dict[str, Any]:
    sku = ctx.sku(id)
    row = _product_rows(ctx, [sku])[0]
    return row if sku in ctx.seen else {"error": f"no product {id} in the catalogue"}


def tool_search(ctx: Context, term: str) -> dict[str, Any]:
    """Ocado's own search: the last resort, for something the household has never bought."""
    from app.scraper.products.ocado import extract_product_objects, normalize_product

    if ctx.searches_left <= 0:
        return {"error": "no searches left for this request: decide from what you have, or ask"}
    with _search_lock:
        now = time.monotonic()
        while _searches and now - _searches[0] > SEARCH_WINDOW:
            _searches.popleft()
        if len(_searches) >= SEARCHES_PER_WINDOW:
            return {"error": "Ocado search is resting (too many lately): decide from what you have, or ask"}
        _searches.append(now)
    ctx.searches_left -= 1
    client = ctx.order().client if ctx._order else ctx.accounts[0][1]
    found = []
    for node in extract_product_objects(client.search(term, size=10))[:10]:
        try:
            p = normalize_product(node)
        except ValueError:
            continue
        ctx.seen[p.sku] = p.name
        found.append({k: v for k, v in {"id": ctx.ref(p.sku), "name": p.name, "brand": p.brand, "pack": p.pack_size_raw,
                                         "price": p.price, "in_stock": p.in_stock}.items() if v is not None})
    return {"products": found}


TOOLS: dict[str, tuple[Callable[..., dict[str, Any]], str, dict[str, Any]]] = {
    "history": (tool_history, "What the household has bought: products whose name, brand or category share a word "
                "with query (omit query for everything), most bought first, with how many orders each was on, "
                "when first and last bought, and a description of the packaging.",
                {"query": {"type": ["string", "null"], "description": "e.g. \"protein bread\""},
                 "since": {"type": ["string", "null"], "description": "only purchases on or after YYYY-MM-DD"}}),
    "mapping": (tool_mapping, "The products accepted for an ingredient name (\"oat milk\", \"chopped tomatoes\"), "
                "with the one that would be bought by default.",
                {"name": {"type": "string"}}),
    "order": (tool_order, "The coming order: its delivery, edit cutoff, every line on it now, and the changes "
              "already planned but not yet made.", {}),
    "product": (tool_product, "One product by id: name, brand, pack size, price, stock, purchase history, packaging.",
                {"id": {"type": "string"}}),
    "search": (tool_search, "Ocado's live search. Last resort, for something never bought and not in a mapping; "
               "at most 2 per request.", {"term": {"type": "string"}}),
}


def tool_specs() -> list[dict[str, Any]]:
    return [{"type": "function", "function": {
        "name": name, "description": doc, "strict": True,
        "parameters": {"type": "object", "properties": props, "required": list(props), "additionalProperties": False},
    }} for name, (_, doc, props) in TOOLS.items()]


def run_tool(ctx: Context, name: str, arguments: str) -> dict[str, Any]:
    if name not in TOOLS:
        return {"error": f"no tool {name}"}
    try:
        args = json.loads(arguments or "{}")
        return TOOLS[name][0](ctx, **{k: v for k, v in args.items() if k in TOOLS[name][2]})
    except top_up.NoOrder:
        raise
    except Exception as exc:  # noqa: BLE001 - the model can work round a failed lookup
        log.info("resolver: %s(%s) failed: %s", name, arguments, exc)
        return {"error": f"{type(exc).__name__}: {exc}"}


# --- the model ---------------------------------------------------------------

_CHANGE = {
    "type": "object",
    "properties": {
        "op": {"type": "string", "enum": list(OPS)},
        "sku": {"type": "string", "description": "the product's id, as the tools gave it (\"p3\")"},
        "quantity": {"type": ["integer", "null"]},
        "replaces": {"type": ["string", "null"], "description": "swap: the id of the product taken off"},
    },
    "required": ["op", "sku", "quantity", "replaces"],
    "additionalProperties": False,
}
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "changes": {"type": "array", "items": _CHANGE},
        "confidence": {"type": "string", "enum": list(CONFIDENCE)},
        "summary": {"type": "string"},
        "question": {"type": ["string", "null"]},
        "options": {"type": "array", "items": {
            "type": "object",
            "properties": {"label": {"type": "string"}, "changes": {"type": "array", "items": _CHANGE}},
            "required": ["label", "changes"], "additionalProperties": False}},
    },
    "required": ["changes", "confidence", "summary", "question", "options"],
    "additionalProperties": False,
}

SYSTEM = """\
You change a household's coming Ocado order from what someone said to their organiser. Work out exactly \
which products they mean with the tools, then answer with the plan.

Their own buying is the best evidence, so start from history (and mapping, for a plain ingredient):
- "the usual X" is the X bought on the most orders; "the other one" or "not the usual one" is the next most \
bought X that isn't the usual.
- "the one we had last month" is the X bought then; "the big pack", "the cheaper one", "the posh one" compare \
pack size, price and brand among the X they buy, or the mapping's products.
- Colours and looks ("green packaging") match the packaging descriptions; use them to confirm or decide.
- A plain item with no qualifier ("oat milk") is what mapping gives as default, or the X they buy most.
- search is the last resort, for something never bought and not in a mapping.

The plan:
- changes is the whole plan, including pending changes that still stand (see order). Merge, don't repeat: \
"actually, two" changes the pending line's quantity; "forget the bread" drops it from the plan.
- op add: quantity more of sku. remove: take sku off. set: make sku's quantity exactly quantity. swap: take \
`replaces` off and add sku (quantity null keeps the old quantity).
- Only touch lines the words are about. Quantity is what was said; for add with no number, 1. sku is \
the product's id exactly as the tools gave it ("p3").
- confidence sure: you know which products. likely: you chose between close alternatives; end summary with \
"…or did you mean <runner-up>?". unsure: you can't tell; changes is the pending plan unchanged, ask a short \
question, and give 2-4 options, each with the whole plan if they pick it.
- summary: one short line, what and why, e.g. "Warburtons Protein Power: your other protein loaf (3 orders), \
green pack". For a question about the order ("what's on the order?"), changes is the pending plan unchanged \
and summary answers it.
- question is null unless unsure; options is [] unless unsure.
"""

#: ``(messages, tools, schema) -> assistant message`` as ``{"content": str | None, "tool_calls":
#: [{"id", "name", "arguments"}]}``. Tests script one; :class:`OpenAIToolModel` is the real one.
ToolModel = Callable[[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]], dict[str, Any]]


class OpenAIToolModel:
    """The Responses API (function tools and reasoning together aren't allowed on chat completions).

    The conversation is kept on OpenAI's side and chained by response id, so each turn sends only what's
    new: the tool results since the last turn. A call with just the system and user messages starts over.
    """

    def __init__(self, model: str | None = None, effort: str | None = None):
        from openai import OpenAI

        if not config.OPENAI_API_KEY:
            raise ResolveError("OPENAI_API_KEY is not set")
        self.model = model or config.RESOLVER_MODEL
        self.effort = effort or config.RESOLVER_EFFORT
        self._client = OpenAI(api_key=config.OPENAI_API_KEY)
        self._previous: str | None = None
        self._sent = 0

    def __call__(self, messages, tools, schema):
        if len(messages) <= 2:
            self._previous, self._sent = None, 0
        new = messages[self._sent:]
        if self._previous is None:
            system = next((m["content"] for m in new if m["role"] == "system"), None)
            items = [{"role": m["role"], "content": m["content"]} for m in new if m["role"] == "user"]
        else:
            system = None
            items = [{"type": "function_call_output", "call_id": m["tool_call_id"], "output": m["content"]}
                     for m in new if m["role"] == "tool"]
        response = self._client.responses.create(
            model=self.model, instructions=system or SYSTEM, input=items, previous_response_id=self._previous,
            reasoning={"effort": self.effort},
            tools=[{"type": "function", **t["function"]} for t in tools],
            text={"format": {"type": "json_schema", "name": "plan", "schema": schema, "strict": True}},
        )
        self._previous, self._sent = response.id, len(messages)
        calls = [{"id": item.call_id, "name": item.name, "arguments": item.arguments}
                 for item in response.output if getattr(item, "type", None) == "function_call"]
        if calls:
            self._sent += 1  # the assistant turn ask_model appends for these calls isn't sent back
        return {"content": None if calls else response.output_text, "tool_calls": calls}


def default_model() -> ToolModel:
    return OpenAIToolModel()


def ask_model(ctx: Context, instructions: list[str], model: ToolModel) -> dict[str, Any]:
    """Run the tool loop until the model answers with a plan."""
    said = "\n".join(f"- {text}" for text in instructions)
    user = (f"Today is {ctx.today:%A %d %B %Y}.\n\nWhat they said (oldest first; the last is new):\n{said}\n\n"
            f"Pending changes, not made yet: {json.dumps(ctx.shown(ctx.pending)) if ctx.pending else 'none'}")
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
    tools = tool_specs()
    for _ in range(MAX_TURNS):
        reply = model(messages, tools, PLAN_SCHEMA)
        calls = reply.get("tool_calls") or []
        if not calls:
            try:
                return json.loads(reply.get("content") or "")
            except ValueError as exc:
                raise ResolveError(f"the model's plan wasn't JSON: {(reply.get('content') or '')[:200]!r}") from exc
        messages.append({"role": "assistant", "content": reply.get("content"), "tool_calls": [
            {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
            for c in calls]})
        for call in calls:
            result = run_tool(ctx, call["name"], call["arguments"])
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)[:12000]})
    raise ResolveError("the model kept looking things up without settling on a plan")


# --- the rules ---------------------------------------------------------------


def _ingredient_words(db: Session, sku: str) -> set[str]:
    names = db.scalars(select(IngredientMapping.name)
                       .join(IngredientMappingProduct, IngredientMappingProduct.mapping_id == IngredientMapping.id)
                       .where(IngredientMapping.retailer == RETAILER, IngredientMappingProduct.sku == sku))
    return set().union(*(purchases.words(n) for n in names))


def _mentioned(ctx: Context, sku: str, words: set[str]) -> bool:
    """Did the words name this product (or its ingredient)? Planned lines count as mentioned."""
    if any(c["sku"] == sku or c.get("replaces") == sku for c in ctx.pending):
        return True
    have = (purchases.words(ctx.seen.get(sku, "")) | _ingredient_words(ctx.db, sku)) - FILLER
    return bool(have & words)


def check(ctx: Context, changes: list[dict[str, Any]], instructions: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """The changes the rules allow, and why any others were refused."""
    words = set().union(*(purchases.words(text) for text in instructions)) - FILLER
    on_order = ctx.order().lines
    planned = {c["sku"] for c in ctx.pending}
    kept, refused = [], []
    for c in changes:
        op, sku, replaces, quantity = c.get("op"), c.get("sku"), c.get("replaces"), c.get("quantity")
        name = ctx.seen.get(sku or "", sku)
        if op not in OPS or not sku:
            refused.append(f"an unknown change ({op})")
            continue
        if sku not in ctx.seen:
            refused.append(f"a product it made up ({sku})")
            continue
        if op == "set" and quantity == 0:
            op, quantity = "remove", None
        if op in ("add", "set") or (op == "swap" and quantity is not None):
            if not isinstance(quantity, int) or not 1 <= quantity <= MAX_QUANTITY:
                if op == "add" and quantity is None:
                    quantity = 1
                else:
                    refused.append(f"{name} ×{quantity} (quantities go from 1 to {MAX_QUANTITY})")
                    continue
        touched = [s for s in ((sku,) if op in ("remove", "set") else (replaces,) if op == "swap" else ()) if s]
        if op == "swap" and (not replaces or replaces not in ctx.seen):
            refused.append(f"swapping in {name} for nothing on the order")
            continue
        bad = [s for s in touched if not (s in on_order or s in planned) or not _mentioned(ctx, s, words)]
        if bad and not (op == "set" and sku not in on_order and _mentioned(ctx, sku, words)):
            refused.append(f"changing {ctx.seen.get(bad[0], bad[0])}, which you didn't mention or isn't on the order")
            continue
        kept.append({"op": op, "sku": sku, "quantity": quantity, "replaces": replaces if op == "swap" else None})
    if len(kept) > MAX_LINES:
        refused.append(f"more than {MAX_LINES} changes at once")
        kept = []
    return kept, refused


def describe(ctx: Context, changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each change with the product names, for the reply."""
    return [{**c, "name": ctx.seen.get(c["sku"], c["sku"]),
             "replaces_name": ctx.seen.get(c["replaces"], c["replaces"]) if c.get("replaces") else None}
            for c in changes]


def said(lines: list[dict[str, Any]]) -> str:
    """"Warburtons Protein Power ×2, bananas off": every change, in words."""
    parts = []
    for c in lines:
        count = f" ×{c['quantity']}" if c.get("quantity") and c["quantity"] > 1 else ""
        parts.append({"add": f"{c['name']}{count}",
                      "remove": f"{c['name']} off",
                      "set": f"{c['name']} to {c.get('quantity')}",
                      "swap": f"{c.get('replaces_name')} → {c['name']}{count}"}[c["op"]])
    return ", ".join(parts)


def plan(db: Session, user_id: int, accounts: list[tuple[RetailerAccount, OcadoClient]], instructions: list[str], *,
         pending: list[dict[str, Any]] | None = None, model: ToolModel | None = None,
         today: date | None = None) -> dict[str, Any]:
    """A proposed change to the coming order for ``instructions`` (oldest first, the last one new),
    merged with ``pending``. Writes nothing to Ocado."""
    ctx = Context(db, user_id, accounts, pending=[dict(c) for c in pending or []], today=today or date.today())
    ctx.names([c["sku"] for c in ctx.pending] + [c["replaces"] for c in ctx.pending if c.get("replaces")])
    order = ctx.order()  # no editable order is worth saying before asking the model anything
    answer = ask_model(ctx, instructions, model or default_model())
    confidence = answer.get("confidence") if answer.get("confidence") in CONFIDENCE else "unsure"
    changes, refused = check(ctx, ctx.unshown(answer.get("changes") or []), instructions)
    question = answer.get("question")
    options = []
    for option in answer.get("options") or []:
        allowed, no = check(ctx, ctx.unshown(option.get("changes") or []), instructions + [option.get("label") or ""])
        if not no:
            options.append({"label": option.get("label") or said(describe(ctx, allowed)),
                            "changes": describe(ctx, allowed)})
    if refused:
        confidence = "unsure"
        question = question or f"I wasn't sure about {'; '.join(refused)}. Can you say it another way?"
    if confidence == "unsure":
        changes = ctx.pending  # nothing changes until they answer
        if not question:
            question = "Which did you mean?"
    else:
        options = []
        question = None
    lines = describe(ctx, changes)
    return {"order": order.about(), "changes": lines, "said": said(lines), "confidence": confidence,
            "summary": answer.get("summary") or "", "question": question, "options": options, "refused": refused}


# --- doing it ----------------------------------------------------------------


def targets(changes: list[dict[str, Any]], have: dict[str, int]) -> dict[str, int]:
    """Absolute quantities for the lines ``changes`` touch, against the order as it is now."""
    target: dict[str, int] = {}

    def now(sku: str) -> int:
        return target.get(sku, have.get(sku, 0))

    for c in changes:
        sku, quantity = c["sku"], c.get("quantity")
        if c["op"] == "add":
            target[sku] = now(sku) + max(1, min(quantity or 1, MAX_QUANTITY))
        elif c["op"] == "remove":
            target[sku] = 0
        elif c["op"] == "set":
            target[sku] = max(0, min(quantity or 0, MAX_QUANTITY))
        elif c["op"] == "swap":
            old = now(c["replaces"])
            target[c["replaces"]] = 0
            target[sku] = now(sku) + max(1, min(quantity or old or 1, MAX_QUANTITY))
    return {sku: q for sku, q in target.items() if q != have.get(sku, 0)}


def apply(db: Session, accounts: list[tuple[RetailerAccount, OcadoClient]], changes: list[dict[str, Any]], *,
          order_id: str | None = None) -> dict[str, Any]:
    """Make ``changes`` to the coming order in one checkout. ``order_id``, if it's still
    editable, is the order the plan was made against; otherwise the next editable one."""
    if not changes:
        raise ValueError("there's nothing to change")
    if len(changes) > MAX_LINES:
        raise ValueError(f"at most {MAX_LINES} changes at once")
    for c in changes:
        if c.get("op") not in OPS or not c.get("sku") or (c["op"] == "swap" and not c.get("replaces")):
            raise ValueError(f"not a change: {c}")
    account, client, raw = _the_order(accounts, order_id)
    oid = str(raw["orderId"])
    have = top_up.order_quantities(client, oid)
    target = targets(changes, have)
    names = dict(db.execute(select(Product.sku, Product.name).where(
        Product.retailer == RETAILER, Product.sku.in_(list(target) + [c["sku"] for c in changes]))).all())
    names.update({c["sku"]: c["name"] for c in changes if c.get("name") and c["sku"] not in names})
    if target:
        amend.change_order(client, oid, quantities=target)
    dates = raw.get("dates") or {}
    return {
        "order": {"order_id": oid, "account": account.email, "delivery_start": dates.get("deliveryStartDate"),
                  "delivery_end": dates.get("deliveryEndDate")},
        "applied": [{"sku": sku, "name": names.get(sku, sku), "before": have.get(sku, 0), "after": q}
                    for sku, q in target.items()],
    }


def _the_order(accounts, order_id: str | None):
    if order_id:
        for account, client in accounts:
            try:
                found = amend.find_order(client, order_id)
            except Exception as exc:  # noqa: BLE001
                log.info("resolver: orders for %s not read: %s", account.key, exc)
                continue
            if found is not None and found.get("editStatus") == "EDITABLE":
                return account, client, found
    return top_up.next_order(accounts)
