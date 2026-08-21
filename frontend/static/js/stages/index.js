const LOADERS = {
  created: () => import("./input.js?v=2026.08.21.042435702251"),
  clarification: () => import("./input.js?v=2026.08.21.042435702251"),
  narrative: () => import("./planning.js?v=2026.08.21.042435702251"),
  outline: () => import("./planning.js?v=2026.08.21.042435702251"),
  sample: () => import("./sample.js?v=2026.08.21.042435702251"),
  deck: () => import("./deck.js?v=2026.08.21.042435702251"),
  review: () => import("./review.js?v=2026.08.21.042435702251"),
  delivery: () => import("./delivery.js?v=2026.08.21.042435702251"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
