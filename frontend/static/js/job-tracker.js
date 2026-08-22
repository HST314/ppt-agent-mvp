import { api } from "./api.js?v=2026.08.22.144845041702";

const TERMINAL = new Set(["succeeded", "failed", "cancelled", "interrupted"]);
const EVENT_TYPES = ["queued", "started", "progress", "checkpoint", "succeeded", "failed", "cancelled", "interrupted", "heartbeat"];

export class JobTracker {
  constructor({ pollInterval = 1000, maxPollInterval = 16000, maxStreamFailures = 2, reconnectBaseDelay = 250, recoveryInterval = 1000, maxRecoveryAttempts = 3 } = {}) {
    this.pollInterval = pollInterval;
    this.maxPollInterval = maxPollInterval;
    this.maxStreamFailures = maxStreamFailures;
    this.reconnectBaseDelay = reconnectBaseDelay;
    this.recoveryInterval = recoveryInterval;
    this.maxRecoveryAttempts = maxRecoveryAttempts;
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
    const track = {
      job, callbacks, source: null, timer: null, reconnectTimer: null, recoveryTimer: null,
      seq: 0, seen: new Set(), stopped: false, streamFailures: 0, recoveryAttempts: 0,
      pollFailures: 0, polling: false,
    };
    this.tracks.set(job.job_id, track);
    callbacks.onUpdate?.(job, "initial");
    this.hydrate(track);
  }

  async hydrate(track) {
    try {
      await this.syncHistory(track);
      const job = await api.getJob(track.job.job_id);
      track.pollFailures = 0;
      track.job = job;
      track.callbacks.onUpdate?.(job, "hydrated");
    } catch (error) {
      this.transportError(track, error, "hydrate");
    }
    if (track.stopped) return;
    if (TERMINAL.has(track.job.status)) return this.finish(track, track.job);
    this.openStream(track);
  }

  async syncHistory(track) {
    const response = await api.jobEventHistory(track.job.job_id, track.seq);
    response.events.forEach((event) => this.applyEvent(track, event, false));
  }

  openStream(track, { recovery = false } = {}) {
    if (track.stopped || track.source) return;
    const source = new EventSource(`/v1/jobs/${encodeURIComponent(track.job.job_id)}/events?after=${track.seq}`);
    track.source = source;
    source.onopen = () => {
      if (track.stopped || track.source !== source) return;
      track.streamFailures = 0;
      track.polling = false;
      if (track.timer) window.clearTimeout(track.timer);
      track.timer = null;
      if (track.recoveryTimer) window.clearTimeout(track.recoveryTimer);
      track.recoveryTimer = null;
      track.callbacks.onTransport?.("sse", { recovered: recovery, seq: track.seq });
    };
    EVENT_TYPES.forEach((type) => source.addEventListener(type, (event) => this.handleEvent(track, event)));
    source.onerror = () => {
      if (track.source !== source) return;
      source.close();
      track.source = null;
      if (recovery) {
        // A recovered stream can fail again after onopen stopped the fallback
        // timer. Reinstate polling before spending another bounded probe so an
        // exhausted recovery budget can never leave the job unobserved.
        track.callbacks.onTransport?.("polling", { reason: "recovered-stream-error", seq: track.seq });
        this.poll(track, true);
        this.scheduleRecovery(track);
        return;
      }
      track.streamFailures += 1;
      if (track.streamFailures < this.maxStreamFailures) {
        const delay = this.reconnectBaseDelay * (2 ** (track.streamFailures - 1));
        track.callbacks.onTransport?.("sse-retry", { attempt: track.streamFailures, delay, seq: track.seq });
        track.reconnectTimer = window.setTimeout(() => {
          track.reconnectTimer = null;
          this.openStream(track);
        }, delay);
        return;
      }
      track.callbacks.onTransport?.("polling", { seq: track.seq });
      this.poll(track, true);
      this.scheduleRecovery(track);
    };
  }

  handleEvent(track, message) {
    const event = JSON.parse(message.data);
    this.applyEvent(track, event, true);
  }

  applyEvent(track, event, reconcile = true) {
    const eventKey = `${event.job_id}:${event.seq}`;
    if (track.seen.has(eventKey) || event.seq <= track.seq) return;
    track.seen.add(eventKey);
    track.seq = event.seq;
    if (event.type === "heartbeat") {
      track.callbacks.onTransport?.("heartbeat", { at: event.at, seq: track.seq });
      return;
    }
    track.callbacks.onEvent?.(event);
    if (reconcile && (TERMINAL.has(event.type) || event.type === "started" || event.type === "checkpoint")) this.reconcile(track);
  }

  poll(track, immediate = false) {
    if (track.stopped || track.timer) return;
    track.polling = true;
    const run = async () => {
      let delay = this.pollInterval;
      track.timer = null;
      if (track.stopped || !track.polling) return;
      try {
        await this.syncHistory(track);
        const job = await api.getJob(track.job.job_id);
        track.pollFailures = 0;
        track.job = job;
        track.callbacks.onUpdate?.(job, "polling");
        if (TERMINAL.has(job.status)) return this.finish(track, job);
      } catch (error) {
        delay = this.transportError(track, error, "polling");
      }
      if (track.polling) track.timer = window.setTimeout(run, delay);
    };
    track.timer = window.setTimeout(run, immediate ? 0 : this.pollInterval);
  }

  scheduleRecovery(track) {
    if (track.stopped || track.recoveryTimer || track.recoveryAttempts >= this.maxRecoveryAttempts) return;
    const delay = this.recoveryInterval * (2 ** track.recoveryAttempts);
    track.recoveryTimer = window.setTimeout(() => {
      track.recoveryTimer = null;
      if (track.stopped || track.source) return;
      track.recoveryAttempts += 1;
      track.callbacks.onTransport?.("sse-recovery", { attempt: track.recoveryAttempts, delay, seq: track.seq });
      this.openStream(track, { recovery: true });
    }, delay);
  }

  async reconcile(track) {
    try {
      await this.syncHistory(track);
      const job = await api.getJob(track.job.job_id);
      track.pollFailures = 0;
      track.job = job;
      track.callbacks.onUpdate?.(job, "reconciled");
      if (TERMINAL.has(job.status)) this.finish(track, job);
    } catch (error) {
      this.transportError(track, error, "reconcile");
      this.poll(track);
    }
  }

  transportError(track, error, channel) {
    track.pollFailures += 1;
    const delay = Math.min(this.pollInterval * (2 ** (track.pollFailures - 1)), this.maxPollInterval);
    track.callbacks.onTransport?.("storage-error", {
      attempt: track.pollFailures,
      delay,
      channel,
      seq: track.seq,
    });
    track.callbacks.onError?.(error, { channel, attempt: track.pollFailures, delay });
    return delay;
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
    track.polling = false;
    track.source?.close();
    if (track.timer) window.clearTimeout(track.timer);
    if (track.reconnectTimer) window.clearTimeout(track.reconnectTimer);
    if (track.recoveryTimer) window.clearTimeout(track.recoveryTimer);
    this.tracks.delete(jobId);
  }

  stopAll() {
    [...this.tracks.keys()].forEach((jobId) => this.stop(jobId));
  }
}
