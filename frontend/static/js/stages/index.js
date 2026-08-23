const LOADERS = {
  created: () => import("./input.js?v=2026.08.23.105055404954"),
  clarification: () => import("./input.js?v=2026.08.23.105055404954"),
  narrative: () => import("./planning.js?v=2026.08.23.105055404954"),
  outline: () => import("./planning.js?v=2026.08.23.105055404954"),
  sample: () => import("./sample.js?v=2026.08.23.105055404954"),
  deck: () => import("./deck.js?v=2026.08.23.105055404954"),
  review: () => import("./review.js?v=2026.08.23.105055404954"),
  delivery: () => import("./delivery.js?v=2026.08.23.105055404954"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
