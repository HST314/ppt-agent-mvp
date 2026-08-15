const LOADERS = {
  created: () => import("./input.js?v=2026.08.15.155434751550"),
  clarification: () => import("./input.js?v=2026.08.15.155434751550"),
  narrative: () => import("./planning.js?v=2026.08.15.155434751550"),
  outline: () => import("./planning.js?v=2026.08.15.155434751550"),
  sample: () => import("./sample.js?v=2026.08.15.155434751550"),
  deck: () => import("./deck.js?v=2026.08.15.155434751550"),
  review: () => import("./review.js?v=2026.08.15.155434751550"),
  delivery: () => import("./delivery.js?v=2026.08.15.155434751550"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
