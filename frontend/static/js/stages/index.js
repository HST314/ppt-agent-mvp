const LOADERS = {
  created: () => import("./input.js?v=2026.08.17.095744983694"),
  clarification: () => import("./input.js?v=2026.08.17.095744983694"),
  narrative: () => import("./planning.js?v=2026.08.17.095744983694"),
  outline: () => import("./planning.js?v=2026.08.17.095744983694"),
  sample: () => import("./sample.js?v=2026.08.17.095744983694"),
  deck: () => import("./deck.js?v=2026.08.17.095744983694"),
  review: () => import("./review.js?v=2026.08.17.095744983694"),
  delivery: () => import("./delivery.js?v=2026.08.17.095744983694"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
