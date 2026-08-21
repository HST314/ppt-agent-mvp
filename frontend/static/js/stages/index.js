const LOADERS = {
  created: () => import("./input.js?v=2026.08.21.105223646308"),
  clarification: () => import("./input.js?v=2026.08.21.105223646308"),
  narrative: () => import("./planning.js?v=2026.08.21.105223646308"),
  outline: () => import("./planning.js?v=2026.08.21.105223646308"),
  sample: () => import("./sample.js?v=2026.08.21.105223646308"),
  deck: () => import("./deck.js?v=2026.08.21.105223646308"),
  review: () => import("./review.js?v=2026.08.21.105223646308"),
  delivery: () => import("./delivery.js?v=2026.08.21.105223646308"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
