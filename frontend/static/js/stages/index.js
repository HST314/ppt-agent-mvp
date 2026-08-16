const LOADERS = {
  created: () => import("./input.js?v=2026.08.17.031746330253"),
  clarification: () => import("./input.js?v=2026.08.17.031746330253"),
  narrative: () => import("./planning.js?v=2026.08.17.031746330253"),
  outline: () => import("./planning.js?v=2026.08.17.031746330253"),
  sample: () => import("./sample.js?v=2026.08.17.031746330253"),
  deck: () => import("./deck.js?v=2026.08.17.031746330253"),
  review: () => import("./review.js?v=2026.08.17.031746330253"),
  delivery: () => import("./delivery.js?v=2026.08.17.031746330253"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
