const LOADERS = {
  created: () => import("./input.js?v=2026.08.17.082310785785"),
  clarification: () => import("./input.js?v=2026.08.17.082310785785"),
  narrative: () => import("./planning.js?v=2026.08.17.082310785785"),
  outline: () => import("./planning.js?v=2026.08.17.082310785785"),
  sample: () => import("./sample.js?v=2026.08.17.082310785785"),
  deck: () => import("./deck.js?v=2026.08.17.082310785785"),
  review: () => import("./review.js?v=2026.08.17.082310785785"),
  delivery: () => import("./delivery.js?v=2026.08.17.082310785785"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
