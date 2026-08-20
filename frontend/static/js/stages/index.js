const LOADERS = {
  created: () => import("./input.js?v=2026.08.20.114142303041"),
  clarification: () => import("./input.js?v=2026.08.20.114142303041"),
  narrative: () => import("./planning.js?v=2026.08.20.114142303041"),
  outline: () => import("./planning.js?v=2026.08.20.114142303041"),
  sample: () => import("./sample.js?v=2026.08.20.114142303041"),
  deck: () => import("./deck.js?v=2026.08.20.114142303041"),
  review: () => import("./review.js?v=2026.08.20.114142303041"),
  delivery: () => import("./delivery.js?v=2026.08.20.114142303041"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
