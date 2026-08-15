const LOADERS = {
  created: () => import("./input.js?v=2026.08.15.084125796211"),
  clarification: () => import("./input.js?v=2026.08.15.084125796211"),
  narrative: () => import("./planning.js?v=2026.08.15.084125796211"),
  outline: () => import("./planning.js?v=2026.08.15.084125796211"),
  sample: () => import("./sample.js?v=2026.08.15.084125796211"),
  deck: () => import("./deck.js?v=2026.08.15.084125796211"),
  review: () => import("./review.js?v=2026.08.15.084125796211"),
  delivery: () => import("./delivery.js?v=2026.08.15.084125796211"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
