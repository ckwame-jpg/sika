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

interface GalaxyDot {
  x: number;
  y: number;
  r: number;
  brightness: number;
  tint: string;
}

interface Galaxy {
  cx: number;
  cy: number;
  scale: number;
  tilt: number;
  aspect: number;
  alpha: number;
  depth: number;
  coreTint: string;
  haloTint: string;
  dots: GalaxyDot[];
}

interface GalaxySpec {
  cxR: number;
  cyR: number;
  scaleR: number;
  tilt: number;
  aspect: number;
  alpha: number;
  depth: number;
  coreTint: string;
  haloTint: string;
  armTint: string;
  armCount: number;
  twist: number;
  dotsPerArm: number;
}

declare global {
  interface Window {
    __sikaSky?: SkyState;
  }
}

function buildSpiralDots(
  scale: number,
  armCount: number,
  twist: number,
  dotsPerArm: number,
  armTint: string,
  dpr: number,
): GalaxyDot[] {
  const dots: GalaxyDot[] = [];
  const turnRange = Math.PI * 3.4;
  for (let arm = 0; arm < armCount; arm++) {
    const armOffset = (arm / armCount) * Math.PI * 2;
    for (let i = 0; i < dotsPerArm; i++) {
      const t = i / dotsPerArm;
      const theta = t * turnRange + armOffset;
      const radius = scale * 0.12 * Math.exp(twist * theta);
      if (radius > scale) break;
      const armWidth = scale * 0.06 * (0.4 + t * 0.8);
      const scatter = (Math.random() - 0.5) * armWidth * 2;
      const tangentScatter = (Math.random() - 0.5) * armWidth * 0.8;
      const cosT = Math.cos(theta);
      const sinT = Math.sin(theta);
      const px = (radius + tangentScatter) * cosT + scatter * -sinT;
      const py = (radius + tangentScatter) * sinT + scatter * cosT;
      const dotR = (Math.random() * 0.9 + 0.35) * dpr;
      const radial = radius / scale;
      const brightness = (1 - radial * 0.55) * (0.55 + Math.random() * 0.45);
      const isHotStar = Math.random() < 0.18;
      dots.push({
        x: px,
        y: py,
        r: dotR,
        brightness,
        tint: isHotStar ? "230,230,255" : armTint,
      });
    }
  }
  return dots;
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
    let galaxies: Galaxy[] = [];

    const galaxySpecs: GalaxySpec[] = [
      {
        cxR: 0.13,
        cyR: 0.24,
        scaleR: 0.115,
        tilt: -0.55,
        aspect: 0.45,
        alpha: 0.78,
        depth: 0.55,
        coreTint: "255,238,205",
        haloTint: "190,165,225",
        armTint: "180,200,245",
        armCount: 2,
        twist: 0.22,
        dotsPerArm: 240,
      },
      {
        cxR: 0.88,
        cyR: 0.78,
        scaleR: 0.095,
        tilt: 0.95,
        aspect: 0.32,
        alpha: 0.65,
        depth: 0.5,
        coreTint: "255,232,200",
        haloTint: "200,160,120",
        armTint: "200,210,240",
        armCount: 3,
        twist: 0.18,
        dotsPerArm: 180,
      },
      {
        cxR: 0.78,
        cyR: 0.18,
        scaleR: 0.062,
        tilt: -1.4,
        aspect: 0.6,
        alpha: 0.55,
        depth: 0.65,
        coreTint: "245,235,210",
        haloTint: "160,150,210",
        armTint: "190,205,240",
        armCount: 2,
        twist: 0.26,
        dotsPerArm: 150,
      },
    ];

    function resize() {
      w = canvas!.width = window.innerWidth * dpr;
      h = canvas!.height = window.innerHeight * dpr;
      canvas!.style.width = window.innerWidth + "px";
      canvas!.style.height = window.innerHeight + "px";

      const count = Math.floor((window.innerWidth * window.innerHeight) / 700);
      stars = Array.from({ length: count }, () => ({
        x0: Math.random() * w,
        y0: Math.random() * h,
        r: Math.random() * 1.3 * dpr + 0.2 * dpr,
        a: Math.min(1, Math.random() * 0.975 + 0.286),
        ph: Math.random() * Math.PI * 2,
        sp: 0.0006 + Math.random() * 0.0018,
        drift: 0.3 + Math.random() * 1.4,
        depth: 0.2 + Math.random() * 1.0,
        bloom: Math.random() < 0.18,
        tint:
          Math.random() < 0.16
            ? "180,140,255"
            : Math.random() < 0.28
              ? "140,220,255"
              : "230,230,255",
      }));

      const longSide = Math.max(w, h);
      galaxies = galaxySpecs.map((spec) => {
        const scale = longSide * spec.scaleR;
        return {
          cx: w * spec.cxR,
          cy: h * spec.cyR,
          scale,
          tilt: spec.tilt,
          aspect: spec.aspect,
          alpha: spec.alpha,
          depth: spec.depth,
          coreTint: spec.coreTint,
          haloTint: spec.haloTint,
          dots: buildSpiralDots(scale, spec.armCount, spec.twist, spec.dotsPerArm, spec.armTint, dpr),
        };
      });
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
      for (const g of galaxies) {
        const cx = g.cx + px * g.depth;
        const cy = g.cy + py * g.depth;
        ctx!.save();
        ctx!.translate(cx, cy);
        ctx!.rotate(g.tilt);
        ctx!.scale(1, g.aspect);

        const halo = ctx!.createRadialGradient(0, 0, 0, 0, 0, g.scale);
        halo.addColorStop(0, `rgba(${g.haloTint},${g.alpha * 0.32})`);
        halo.addColorStop(0.45, `rgba(${g.haloTint},${g.alpha * 0.10})`);
        halo.addColorStop(1, `rgba(${g.haloTint},0)`);
        ctx!.fillStyle = halo;
        ctx!.beginPath();
        ctx!.arc(0, 0, g.scale, 0, Math.PI * 2);
        ctx!.fill();

        for (const d of g.dots) {
          ctx!.globalAlpha = Math.min(1, d.brightness * g.alpha);
          ctx!.fillStyle = `rgba(${d.tint},1)`;
          ctx!.beginPath();
          ctx!.arc(d.x, d.y, d.r, 0, Math.PI * 2);
          ctx!.fill();
        }
        ctx!.globalAlpha = 1;

        const coreSize = g.scale * 0.34;
        const core = ctx!.createRadialGradient(0, 0, 0, 0, 0, coreSize);
        core.addColorStop(0, `rgba(${g.coreTint},${Math.min(1, g.alpha * 1.2)})`);
        core.addColorStop(0.35, `rgba(${g.coreTint},${g.alpha * 0.55})`);
        core.addColorStop(1, `rgba(${g.coreTint},0)`);
        ctx!.fillStyle = core;
        ctx!.beginPath();
        ctx!.arc(0, 0, coreSize, 0, Math.PI * 2);
        ctx!.fill();

        const hotSize = g.scale * 0.08;
        const hot = ctx!.createRadialGradient(0, 0, 0, 0, 0, hotSize);
        hot.addColorStop(0, `rgba(255,250,235,${Math.min(1, g.alpha * 1.6)})`);
        hot.addColorStop(1, `rgba(255,250,235,0)`);
        ctx!.fillStyle = hot;
        ctx!.beginPath();
        ctx!.arc(0, 0, hotSize, 0, Math.PI * 2);
        ctx!.fill();

        ctx!.restore();
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
