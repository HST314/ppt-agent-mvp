const LOADERS = {
  created: () => import("./input.js?v=2026.08.20.152614537731"),
  clarification: () => import("./input.js?v=2026.08.20.152614537731"),
  narrative: () => import("./planning.js?v=2026.08.20.152614537731"),
  outline: () => import("./planning.js?v=2026.08.20.152614537731"),
  sample: () => import("./sample.js?v=2026.08.20.152614537731"),
  deck: () => import("./deck.js?v=2026.08.20.152614537731"),
  review: () => import("./review.js?v=2026.08.20.152614537731"),
  delivery: () => import("./delivery.js?v=2026.08.20.152614537731"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
