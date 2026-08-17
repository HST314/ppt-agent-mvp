const LOADERS = {
  created: () => import("./input.js?v=2026.08.17.062039223427"),
  clarification: () => import("./input.js?v=2026.08.17.062039223427"),
  narrative: () => import("./planning.js?v=2026.08.17.062039223427"),
  outline: () => import("./planning.js?v=2026.08.17.062039223427"),
  sample: () => import("./sample.js?v=2026.08.17.062039223427"),
  deck: () => import("./deck.js?v=2026.08.17.062039223427"),
  review: () => import("./review.js?v=2026.08.17.062039223427"),
  delivery: () => import("./delivery.js?v=2026.08.17.062039223427"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
