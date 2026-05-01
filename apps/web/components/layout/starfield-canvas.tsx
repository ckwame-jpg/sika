"use client";

import { useEffect, useRef } from "react";

interface SkyState {
  ox: number;
  oy: number;
  tx: number;
  ty: number;
  dragging: boolean;
  startOx: number;
  startOy: number;
}

interface Star {
  x0: number;
  y0: number;
  r: number;
  a: number;
  ph: number;
  sp: number;
  drift: number;
  depth: number;
  bloom: boolean;
  tint: string;
}

interface Nebula {
  cx: number;
  cy: number;
  r: number;
  tint: string;
  alpha: number;
  sp: number;
  ph: number;
  depth: number;
}

declare global {
  interface Window {
    __sikaSky?: SkyState;
  }
}

export function StarfieldCanvas() {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
    if (reduced.matches) return;

    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    if (!window.__sikaSky) {
      window.__sikaSky = { ox: 0, oy: 0, tx: 0, ty: 0, dragging: false, startOx: 0, startOy: 0 };
    }
    const sky: SkyState = window.__sikaSky;

    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    let w = 0;
    let h = 0;
    let stars: Star[] = [];
    let nebulae: Nebula[] = [];

    function resize() {
      w = canvas!.width = window.innerWidth * dpr;
      h = canvas!.height = window.innerHeight * dpr;
      canvas!.style.width = window.innerWidth + "px";
      canvas!.style.height = window.innerHeight + "px";
      const count = Math.floor((window.innerWidth * window.innerHeight) / 1600);
      stars = Array.from({ length: count }, () => ({
        x0: Math.random() * w,
        y0: Math.random() * h,
        r: Math.random() * 1.3 * dpr + 0.22 * dpr,
        a: Math.min(1, Math.random() * 0.975 + 0.286),
        ph: Math.random() * Math.PI * 2,
        sp: 0.0006 + Math.random() * 0.0018,
        drift: 0.3 + Math.random() * 1.4,
        depth: 0.2 + Math.random() * 1.0,
        bloom: Math.random() < 0.22,
        tint:
          Math.random() < 0.16
            ? "180,140,255"
            : Math.random() < 0.28
              ? "140,220,255"
              : "230,230,255",
      }));
      nebulae = [
        { cx: w * 0.72, cy: h * 0.18, r: Math.max(w, h) * 0.40, tint: "120,70,220", alpha: 0.28, sp: 0.00003, ph: 0.3, depth: 0.3 },
        { cx: w * 0.18, cy: h * 0.82, r: Math.max(w, h) * 0.36, tint: "60,160,220", alpha: 0.16, sp: 0.00002, ph: 1.7, depth: 0.25 },
        { cx: w * 0.55, cy: h * 0.55, r: Math.max(w, h) * 0.30, tint: "180,120,255", alpha: 0.10, sp: 0.00004, ph: 2.2, depth: 0.4 },
        { cx: w * 0.88, cy: h * 0.62, r: Math.max(w, h) * 0.22, tint: "100,200,255", alpha: 0.08, sp: 0.00003, ph: 3.7, depth: 0.5 },
      ];
    }

    let raf = 0;
    const t0 = performance.now();
    function frame(t: number) {
      const dt = t - t0;
      sky.ox += sky.ox * -0.012;
      const px = (sky.ox + sky.tx) * dpr;
      const py = (sky.oy + sky.ty) * dpr;

      ctx!.clearRect(0, 0, w, h);
      ctx!.globalCompositeOperation = "screen";
      for (const n of nebulae) {
        const pulse = 0.85 + 0.15 * Math.sin(n.ph + dt * n.sp * 20);
        const cx = n.cx + px * n.depth + Math.sin(dt * n.sp + n.ph) * 40 * dpr;
        const cy = n.cy + py * n.depth + Math.cos(dt * n.sp * 1.3 + n.ph) * 30 * dpr;
        const grad = ctx!.createRadialGradient(cx, cy, 0, cx, cy, n.r * pulse);
        grad.addColorStop(0, `rgba(${n.tint},${n.alpha})`);
        grad.addColorStop(0.5, `rgba(${n.tint},${n.alpha * 0.35})`);
        grad.addColorStop(1, `rgba(${n.tint},0)`);
        ctx!.fillStyle = grad;
        ctx!.beginPath();
        ctx!.arc(cx, cy, n.r * pulse, 0, Math.PI * 2);
        ctx!.fill();
      }
      ctx!.globalCompositeOperation = "source-over";

      for (const s of stars) {
        const warpX = Math.sin(dt * 0.00015 + s.y0 * 0.0013) * s.drift * dpr;
        const warpY = Math.cos(dt * 0.00012 + s.x0 * 0.0011) * s.drift * dpr * 0.6;
        const x = s.x0 + warpX + px * s.depth;
        const y = s.y0 + warpY + py * s.depth;
        const tw = 0.55 + 0.45 * Math.sin(s.ph + dt * s.sp);
        const a = s.a * tw;
        if (s.bloom) {
          const bg = ctx!.createRadialGradient(x, y, 0, x, y, s.r * 7);
          bg.addColorStop(0, `rgba(${s.tint},${a * 0.40})`);
          bg.addColorStop(1, `rgba(${s.tint},0)`);
          ctx!.fillStyle = bg;
          ctx!.beginPath();
          ctx!.arc(x, y, s.r * 7, 0, Math.PI * 2);
          ctx!.fill();
        }
        ctx!.globalAlpha = a;
        ctx!.fillStyle = `rgba(${s.tint},1)`;
        ctx!.beginPath();
        ctx!.arc(x, y, s.r, 0, Math.PI * 2);
        ctx!.fill();
      }
      ctx!.globalAlpha = 1;
      raf = requestAnimationFrame(frame);
    }

    let lx = 0;
    let ly = 0;
    const onMove = (e: MouseEvent) => {
      const cx = e.clientX / window.innerWidth - 0.5;
      const cy = e.clientY / window.innerHeight - 0.5;
      sky.tx = cx * 20;
      sky.ty = cy * 14;
    };
    const onPointerDown = (e: PointerEvent) => {
      const tgt = e.target;
      if (!(tgt instanceof Element)) return;
      if (
        tgt.closest(
          "a, button, input, select, textarea, label, [role=button], .sidebar, .analyst-trigger, .topbar",
        )
      ) {
        return;
      }
      sky.dragging = true;
      sky.startOx = sky.ox;
      sky.startOy = sky.oy;
      lx = e.clientX;
      ly = e.clientY;
      document.body.style.cursor = "grabbing";
      const hint = document.getElementById("orbitHint");
      if (hint) hint.classList.add("hide");
    };
    const onPointerMove = (e: PointerEvent) => {
      if (!sky.dragging) return;
      sky.ox = sky.startOx + (e.clientX - lx) * 0.6;
      sky.oy = sky.startOy + (e.clientY - ly) * 0.6;
    };
    const onPointerUp = () => {
      sky.dragging = false;
      document.body.style.cursor = "";
    };

    resize();
    window.addEventListener("resize", resize);
    window.addEventListener("mousemove", onMove, { passive: true });
    window.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp);
    window.addEventListener("pointercancel", onPointerUp);
    raf = requestAnimationFrame(frame);

    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", resize);
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", onPointerUp);
      window.removeEventListener("pointercancel", onPointerUp);
    };
  }, []);

  return <canvas ref={canvasRef} className="page-canvas" aria-hidden />;
}
