const LOADERS = {
  created: () => import("./input.js?v=2026.08.23.093634439968"),
  clarification: () => import("./input.js?v=2026.08.23.093634439968"),
  narrative: () => import("./planning.js?v=2026.08.23.093634439968"),
  outline: () => import("./planning.js?v=2026.08.23.093634439968"),
  sample: () => import("./sample.js?v=2026.08.23.093634439968"),
  deck: () => import("./deck.js?v=2026.08.23.093634439968"),
  review: () => import("./review.js?v=2026.08.23.093634439968"),
  delivery: () => import("./delivery.js?v=2026.08.23.093634439968"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
