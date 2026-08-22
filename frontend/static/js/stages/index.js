const LOADERS = {
  created: () => import("./input.js?v=2026.08.22.110501195536"),
  clarification: () => import("./input.js?v=2026.08.22.110501195536"),
  narrative: () => import("./planning.js?v=2026.08.22.110501195536"),
  outline: () => import("./planning.js?v=2026.08.22.110501195536"),
  sample: () => import("./sample.js?v=2026.08.22.110501195536"),
  deck: () => import("./deck.js?v=2026.08.22.110501195536"),
  review: () => import("./review.js?v=2026.08.22.110501195536"),
  delivery: () => import("./delivery.js?v=2026.08.22.110501195536"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
