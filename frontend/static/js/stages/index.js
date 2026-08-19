const LOADERS = {
  created: () => import("./input.js?v=2026.08.19.043945581370"),
  clarification: () => import("./input.js?v=2026.08.19.043945581370"),
  narrative: () => import("./planning.js?v=2026.08.19.043945581370"),
  outline: () => import("./planning.js?v=2026.08.19.043945581370"),
  sample: () => import("./sample.js?v=2026.08.19.043945581370"),
  deck: () => import("./deck.js?v=2026.08.19.043945581370"),
  review: () => import("./review.js?v=2026.08.19.043945581370"),
  delivery: () => import("./delivery.js?v=2026.08.19.043945581370"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
