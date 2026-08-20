const LOADERS = {
  created: () => import("./input.js?v=2026.08.20.141243404257"),
  clarification: () => import("./input.js?v=2026.08.20.141243404257"),
  narrative: () => import("./planning.js?v=2026.08.20.141243404257"),
  outline: () => import("./planning.js?v=2026.08.20.141243404257"),
  sample: () => import("./sample.js?v=2026.08.20.141243404257"),
  deck: () => import("./deck.js?v=2026.08.20.141243404257"),
  review: () => import("./review.js?v=2026.08.20.141243404257"),
  delivery: () => import("./delivery.js?v=2026.08.20.141243404257"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
