const LOADERS = {
  created: () => import("./input.js?v=2026.08.24.091507484151"),
  clarification: () => import("./input.js?v=2026.08.24.091507484151"),
  narrative: () => import("./planning.js?v=2026.08.24.091507484151"),
  outline: () => import("./planning.js?v=2026.08.24.091507484151"),
  sample: () => import("./sample.js?v=2026.08.24.091507484151"),
  deck: () => import("./deck.js?v=2026.08.24.091507484151"),
  review: () => import("./review.js?v=2026.08.24.091507484151"),
  delivery: () => import("./delivery.js?v=2026.08.24.091507484151"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
