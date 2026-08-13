import { api } from "./api.js";

const TERMINAL = new Set(["succeeded", "failed", "cancelled", "interrupted"]);
const EVENT_TYPES = ["queued", "started", "progress", "checkpoint", "succeeded", "failed", "cancelled", "interrupted", "heartbeat"];

export class JobTracker {
  constructor({ pollInterval = 2000 } = {}) {
    this.pollInterval = pollInterval;
    this.tracks = new Map();
    this.completed = new Set();
  }

  async discover(taskId, callbacks = {}) {
    const response = await api.activeJobs(taskId);
    response.jobs.forEach((job) => this.track(job, callbacks));
    return response.jobs;
  }

  track(job, callbacks = {}) {
    this.stop(job.job_id);
    const track = { job, callbacks, source: null, timer: null, seq: job.last_seq || 0, stopped: false };
    this.tracks.set(job.job_id, track);
    callbacks.onUpdate?.(job, "initial");
    if (TERMINAL.has(job.status)) return this.finish(track, job);
    this.openStream(track);
  }

  openStream(track) {
    if (track.stopped) return;
    const source = new EventSource(`/v1/jobs/${encodeURIComponent(track.job.job_id)}/events?after=${track.seq}`);
    track.source = source;
    EVENT_TYPES.forEach((type) => source.addEventListener(type, (event) => this.handleEvent(track, event)));
    source.onerror = () => {
      source.close();
      track.source = null;
      this.poll(track);
    };
  }

  handleEvent(track, message) {
    const event = JSON.parse(message.data);
    if (event.seq <= track.seq) return;
    track.seq = event.seq;
    track.callbacks.onEvent?.(event);
    if (TERMINAL.has(event.type)) this.reconcile(track);
  }

  poll(track) {
    if (track.stopped || track.timer) return;
    const run = async () => {
      track.timer = null;
      if (track.stopped) return;
      try {
        const job = await api.getJob(track.job.job_id);
        track.job = job;
        track.seq = Math.max(track.seq, job.last_seq || 0);
        track.callbacks.onUpdate?.(job, "polling");
        if (TERMINAL.has(job.status)) return this.finish(track, job);
      } catch (error) {
        track.callbacks.onError?.(error);
      }
      track.timer = window.setTimeout(run, this.pollInterval);
    };
    track.timer = window.setTimeout(run, this.pollInterval);
  }

  async reconcile(track) {
    try {
      const job = await api.getJob(track.job.job_id);
      track.job = job;
      track.callbacks.onUpdate?.(job, "reconciled");
      if (TERMINAL.has(job.status)) this.finish(track, job);
    } catch (error) {
      track.callbacks.onError?.(error);
      this.poll(track);
    }
  }

  finish(track, job) {
    if (this.completed.has(job.job_id)) return;
    this.completed.add(job.job_id);
    this.stop(job.job_id);
    track.callbacks.onComplete?.(job);
  }

  stop(jobId) {
    const track = this.tracks.get(jobId);
    if (!track) return;
    track.stopped = true;
    track.source?.close();
    if (track.timer) window.clearTimeout(track.timer);
    this.tracks.delete(jobId);
  }

  stopAll() {
    [...this.tracks.keys()].forEach((jobId) => this.stop(jobId));
  }
}
