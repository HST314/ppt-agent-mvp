const LOADERS = {
  created: () => import("./input.js?v=2026.08.16.113005185829"),
  clarification: () => import("./input.js?v=2026.08.16.113005185829"),
  narrative: () => import("./planning.js?v=2026.08.16.113005185829"),
  outline: () => import("./planning.js?v=2026.08.16.113005185829"),
  sample: () => import("./sample.js?v=2026.08.16.113005185829"),
  deck: () => import("./deck.js?v=2026.08.16.113005185829"),
  review: () => import("./review.js?v=2026.08.16.113005185829"),
  delivery: () => import("./delivery.js?v=2026.08.16.113005185829"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
