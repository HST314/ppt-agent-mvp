const LOADERS = {
  created: () => import("./input.js?v=2026.08.21.045824180656"),
  clarification: () => import("./input.js?v=2026.08.21.045824180656"),
  narrative: () => import("./planning.js?v=2026.08.21.045824180656"),
  outline: () => import("./planning.js?v=2026.08.21.045824180656"),
  sample: () => import("./sample.js?v=2026.08.21.045824180656"),
  deck: () => import("./deck.js?v=2026.08.21.045824180656"),
  review: () => import("./review.js?v=2026.08.21.045824180656"),
  delivery: () => import("./delivery.js?v=2026.08.21.045824180656"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
