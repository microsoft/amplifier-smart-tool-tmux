// Keyboard input has its own ordered queue. A slow tool response must not stop
// xterm accepting keys, and a continuous typing burst must not reset the timer.
export class LiveInput {
  constructor({ send, failed, delay = 12, limit = 1024 * 1024 }) {
    Object.assign(this, { send, failed, delay, limit });
    this.encoder = new TextEncoder();
    this.parts = [];
    this.size = 0;
    this.epoch = 0;
    this.timer = null;
    this.running = null;
  }

  get active() {
    return !!this.running || this.size > 0;
  }

  clear() {
    this.epoch++;
    clearTimeout(this.timer);
    this.timer = null;
    this.parts = [];
    this.size = 0;
  }

  push(text, authority) {
    const bytes = this.encoder.encode(text);
    if (this.size + bytes.length > this.limit) {
      this.clear();
      this.failed(
        Error(
          "The terminal connection cannot keep up. Unsent input was discarded.",
        ),
      );
      return;
    }
    // Split only at UTF-8 character boundaries, including four-byte emoji.
    for (let start = 0; start < bytes.length; ) {
      let end = Math.min(start + 4096, bytes.length);
      while (end < bytes.length && (bytes[end] & 0xc0) === 0x80) end--;
      this.parts.push({ bytes: bytes.slice(start, end), authority });
      start = end;
    }
    this.size += bytes.length;
    if (!this.timer && !this.running)
      this.timer = setTimeout(() => this.flush(), this.delay);
  }

  async flush() {
    clearTimeout(this.timer);
    this.timer = null;
    if (this.running) return this.running;
    this.running = this.drain();
    try {
      await this.running;
    } finally {
      this.running = null;
      if (this.size && !this.timer)
        this.timer = setTimeout(() => this.flush(), this.delay);
    }
  }

  async drain() {
    while (this.parts.length) {
      const epoch = this.epoch;
      const first = this.parts.shift();
      const chunks = [first.bytes];
      let length = first.bytes.length;
      while (
        this.parts.length &&
        length + this.parts[0].bytes.length <= 4096 &&
        this.parts[0].authority === first.authority
      ) {
        const part = this.parts.shift();
        chunks.push(part.bytes);
        length += part.bytes.length;
      }
      this.size -= length;
      const bytes = new Uint8Array(length);
      let offset = 0;
      for (const chunk of chunks) {
        bytes.set(chunk, offset);
        offset += chunk.length;
      }
      try {
        await this.send(bytes, first.authority, () => epoch === this.epoch);
      } catch (error) {
        // A pane switch may already have invalidated this in-flight result.
        if (epoch === this.epoch) {
          this.clear();
          this.failed(error);
        }
      }
    }
  }
}
