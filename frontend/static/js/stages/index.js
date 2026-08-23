const LOADERS = {
  created: () => import("./input.js?v=2026.08.23.102655140222"),
  clarification: () => import("./input.js?v=2026.08.23.102655140222"),
  narrative: () => import("./planning.js?v=2026.08.23.102655140222"),
  outline: () => import("./planning.js?v=2026.08.23.102655140222"),
  sample: () => import("./sample.js?v=2026.08.23.102655140222"),
  deck: () => import("./deck.js?v=2026.08.23.102655140222"),
  review: () => import("./review.js?v=2026.08.23.102655140222"),
  delivery: () => import("./delivery.js?v=2026.08.23.102655140222"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
