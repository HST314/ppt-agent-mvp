const LOADERS = {
  created: () => import("./input.js?v=2026.08.21.035240047774"),
  clarification: () => import("./input.js?v=2026.08.21.035240047774"),
  narrative: () => import("./planning.js?v=2026.08.21.035240047774"),
  outline: () => import("./planning.js?v=2026.08.21.035240047774"),
  sample: () => import("./sample.js?v=2026.08.21.035240047774"),
  deck: () => import("./deck.js?v=2026.08.21.035240047774"),
  review: () => import("./review.js?v=2026.08.21.035240047774"),
  delivery: () => import("./delivery.js?v=2026.08.21.035240047774"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
