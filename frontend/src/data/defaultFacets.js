const facet = (value, label) => ({ value, label, count: 0 })

export const DEFAULT_FACETS = {
  cuisines: [],
  diets: [
    facet('vegetarian', 'Vegetarian'),
    facet('pescatarian', 'Pescatarian'),
    facet('dairy_free', 'Dairy-free'),
    facet('gluten_free', 'Gluten-free'),
    facet('low_carb', 'Low carb'),
  ],
  attributes: [
    facet('high-protein', 'High protein'),
    facet('quick', 'Quick'),
    facet('super-quick', 'Super quick'),
    facet('family-friendly', 'Family friendly'),
    facet('spicy', 'Spicy'),
    facet('calorie-smart', 'Calorie smart'),
    facet('healthy', 'Healthy'),
  ],
  proteins: [
    facet('chicken', 'Chicken'),
    facet('beef', 'Beef'),
    facet('pork', 'Pork'),
    facet('lamb', 'Lamb'),
    facet('turkey', 'Turkey'),
    facet('duck', 'Duck'),
    facet('fish', 'Fish'),
    facet('prawn', 'Prawns'),
    facet('tofu', 'Tofu'),
    facet('halloumi', 'Halloumi'),
  ],
  excludes: [],
  ranges: {
    kcal: { min: 0, max: 1500 },
    protein: { min: 0, max: 80 },
    protein_ratio: { min: 0, max: 12 },
    time: { min: 0, max: 90 },
  },
  sorts: [
    facet('popular', 'Most popular'),
    facet('rating', 'Highest rated'),
    facet('protein_high', 'Most protein'),
    facet('protein_ratio', 'Most protein per calorie'),
    facet('kcal_low', 'Fewest calories'),
    facet('time_low', 'Quickest'),
    facet('newest', 'Newest'),
  ],
}
