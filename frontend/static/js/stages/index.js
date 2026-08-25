const LOADERS = {
  created: () => import("./input.js?v=2026.08.25.023156421530"),
  clarification: () => import("./input.js?v=2026.08.25.023156421530"),
  narrative: () => import("./planning.js?v=2026.08.25.023156421530"),
  outline: () => import("./planning.js?v=2026.08.25.023156421530"),
  sample: () => import("./sample.js?v=2026.08.25.023156421530"),
  deck: () => import("./deck.js?v=2026.08.25.023156421530"),
  review: () => import("./review.js?v=2026.08.25.023156421530"),
  delivery: () => import("./delivery.js?v=2026.08.25.023156421530"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
