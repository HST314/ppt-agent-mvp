const LOADERS = {
  created: () => import("./input.js?v=2026.08.16.100923465190"),
  clarification: () => import("./input.js?v=2026.08.16.100923465190"),
  narrative: () => import("./planning.js?v=2026.08.16.100923465190"),
  outline: () => import("./planning.js?v=2026.08.16.100923465190"),
  sample: () => import("./sample.js?v=2026.08.16.100923465190"),
  deck: () => import("./deck.js?v=2026.08.16.100923465190"),
  review: () => import("./review.js?v=2026.08.16.100923465190"),
  delivery: () => import("./delivery.js?v=2026.08.16.100923465190"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
