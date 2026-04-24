/**
 * maid_sprite.js — Reusable sprite animation toolkit for maidanims sheets.
 *
 * Usage:
 *   const player = new MaidSprite(element, {
 *     src: '/static/assets/maidanims/Pack/file.png',
 *     cols: 10, rows: 9, fw: 64, fh: 64,
 *     fps: 8, row: 0, scale: 2
 *   });
 *   player.setRow(2);
 *   player.setFPS(12);
 *   player.stop(); player.play();
 *   player.destroy();
 *
 * Auto-init via data attributes:
 *   <div class="maid-sprite"
 *     data-src="/static/assets/maidanims/..."
 *     data-cols="10" data-rows="9"
 *     data-fw="64" data-fh="64"
 *     data-fps="8" data-row="0" data-scale="2">
 *   </div>
 *   MaidSprite.initAll();
 */
class MaidSprite {
  constructor(el, opts = {}) {
    this.el = el;
    this.cols  = opts.cols  || 10;
    this.rows  = opts.rows  || 1;
    this.fw    = opts.fw    || 64;
    this.fh    = opts.fh    || 64;
    this.fps   = opts.fps   || 8;
    this.row   = Math.min(opts.row || 0, this.rows - 1);
    this.scale = opts.scale || 1;
    this.frame = 0;
    this._timer = null;

    const dw = this.fw * this.scale;
    const dh = this.fh * this.scale;

    el.style.width           = dw + 'px';
    el.style.height          = dh + 'px';
    el.style.backgroundImage = `url('${opts.src}')`;
    el.style.backgroundRepeat = 'no-repeat';
    el.style.imageRendering  = 'pixelated';
    el.style.backgroundSize  = `${this.cols * dw}px auto`;
    el.style.display         = 'inline-block';

    this._seek();
    this.play();
  }

  _seek() {
    const dw = this.fw * this.scale;
    const dh = this.fh * this.scale;
    this.el.style.backgroundPosition =
      `-${this.frame * dw}px -${this.row * dh}px`;
  }

  play() {
    if (this._timer) return;
    this._timer = setInterval(() => {
      this.frame = (this.frame + 1) % this.cols;
      this._seek();
    }, 1000 / this.fps);
  }

  stop() {
    clearInterval(this._timer);
    this._timer = null;
  }

  setRow(r) {
    this.row   = Math.min(r, this.rows - 1);
    this.frame = 0;
    this._seek();
  }

  setFPS(fps) {
    this.fps = fps;
    if (this._timer) { this.stop(); this.play(); }
  }

  setScale(s) {
    this.scale = s;
    const dw = this.fw * s;
    const dh = this.fh * s;
    this.el.style.width          = dw + 'px';
    this.el.style.height         = dh + 'px';
    this.el.style.backgroundSize = `${this.cols * dw}px auto`;
    this._seek();
  }

  destroy() {
    this.stop();
    this.el.style.backgroundImage = '';
  }

  /** Auto-initialise all .maid-sprite elements with data-* attributes */
  static initAll(root = document) {
    root.querySelectorAll('.maid-sprite[data-src]').forEach(el => {
      if (el._maidSprite) return;
      const d = el.dataset;
      el._maidSprite = new MaidSprite(el, {
        src:   d.src,
        cols:  +d.cols  || 10,
        rows:  +d.rows  || 1,
        fw:    +d.fw    || 64,
        fh:    +d.fh    || 64,
        fps:   +d.fps   || 8,
        row:   +d.row   || 0,
        scale: +d.scale || 1,
      });
    });
  }
}

document.addEventListener('DOMContentLoaded', () => MaidSprite.initAll());
