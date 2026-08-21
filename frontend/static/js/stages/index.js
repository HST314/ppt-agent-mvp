const LOADERS = {
  created: () => import("./input.js?v=2026.08.21.115749201866"),
  clarification: () => import("./input.js?v=2026.08.21.115749201866"),
  narrative: () => import("./planning.js?v=2026.08.21.115749201866"),
  outline: () => import("./planning.js?v=2026.08.21.115749201866"),
  sample: () => import("./sample.js?v=2026.08.21.115749201866"),
  deck: () => import("./deck.js?v=2026.08.21.115749201866"),
  review: () => import("./review.js?v=2026.08.21.115749201866"),
  delivery: () => import("./delivery.js?v=2026.08.21.115749201866"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
