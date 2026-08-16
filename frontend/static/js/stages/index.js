const LOADERS = {
  created: () => import("./input.js?v=2026.08.16.053640953906"),
  clarification: () => import("./input.js?v=2026.08.16.053640953906"),
  narrative: () => import("./planning.js?v=2026.08.16.053640953906"),
  outline: () => import("./planning.js?v=2026.08.16.053640953906"),
  sample: () => import("./sample.js?v=2026.08.16.053640953906"),
  deck: () => import("./deck.js?v=2026.08.16.053640953906"),
  review: () => import("./review.js?v=2026.08.16.053640953906"),
  delivery: () => import("./delivery.js?v=2026.08.16.053640953906"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
