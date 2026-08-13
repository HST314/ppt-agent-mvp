const LOADERS = {
  created: () => import("./input.js"),
  clarification: () => import("./input.js"),
  narrative: () => import("./planning.js"),
  outline: () => import("./planning.js"),
  sample: () => import("./sample.js"),
  deck: () => import("./deck.js"),
  review: () => import("./review.js"),
  delivery: () => import("./delivery.js"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
