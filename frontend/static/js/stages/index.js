const LOADERS = {
  created: () => import("./input.js?v=2026.08.15.092035798481"),
  clarification: () => import("./input.js?v=2026.08.15.092035798481"),
  narrative: () => import("./planning.js?v=2026.08.15.092035798481"),
  outline: () => import("./planning.js?v=2026.08.15.092035798481"),
  sample: () => import("./sample.js?v=2026.08.15.092035798481"),
  deck: () => import("./deck.js?v=2026.08.15.092035798481"),
  review: () => import("./review.js?v=2026.08.15.092035798481"),
  delivery: () => import("./delivery.js?v=2026.08.15.092035798481"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
