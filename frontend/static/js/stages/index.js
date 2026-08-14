const LOADERS = {
  created: () => import("./input.js?v=2026.08.14.1"),
  clarification: () => import("./input.js?v=2026.08.14.1"),
  narrative: () => import("./planning.js?v=2026.08.14.1"),
  outline: () => import("./planning.js?v=2026.08.14.1"),
  sample: () => import("./sample.js?v=2026.08.14.1"),
  deck: () => import("./deck.js?v=2026.08.14.1"),
  review: () => import("./review.js?v=2026.08.14.1"),
  delivery: () => import("./delivery.js?v=2026.08.14.1"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
