const LOADERS = {
  created: () => import("./input.js?v=2026.08.14.3"),
  clarification: () => import("./input.js?v=2026.08.14.3"),
  narrative: () => import("./planning.js?v=2026.08.14.3"),
  outline: () => import("./planning.js?v=2026.08.14.3"),
  sample: () => import("./sample.js?v=2026.08.14.3"),
  deck: () => import("./deck.js?v=2026.08.14.3"),
  review: () => import("./review.js?v=2026.08.14.3"),
  delivery: () => import("./delivery.js?v=2026.08.14.3"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
