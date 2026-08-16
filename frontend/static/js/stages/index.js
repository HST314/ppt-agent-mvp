const LOADERS = {
  created: () => import("./input.js?v=2026.08.16.064435603168"),
  clarification: () => import("./input.js?v=2026.08.16.064435603168"),
  narrative: () => import("./planning.js?v=2026.08.16.064435603168"),
  outline: () => import("./planning.js?v=2026.08.16.064435603168"),
  sample: () => import("./sample.js?v=2026.08.16.064435603168"),
  deck: () => import("./deck.js?v=2026.08.16.064435603168"),
  review: () => import("./review.js?v=2026.08.16.064435603168"),
  delivery: () => import("./delivery.js?v=2026.08.16.064435603168"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
