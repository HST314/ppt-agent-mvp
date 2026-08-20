const LOADERS = {
  created: () => import("./input.js?v=2026.08.20.130612827541"),
  clarification: () => import("./input.js?v=2026.08.20.130612827541"),
  narrative: () => import("./planning.js?v=2026.08.20.130612827541"),
  outline: () => import("./planning.js?v=2026.08.20.130612827541"),
  sample: () => import("./sample.js?v=2026.08.20.130612827541"),
  deck: () => import("./deck.js?v=2026.08.20.130612827541"),
  review: () => import("./review.js?v=2026.08.20.130612827541"),
  delivery: () => import("./delivery.js?v=2026.08.20.130612827541"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
