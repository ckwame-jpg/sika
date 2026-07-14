"use client";

import { useEffect, useRef } from "react";

interface Streak {
  x: number;
  y: number;
  vx: number;
  vy: number;
  life: number;
  maxLife: number;
  length: number;
}

const MIN_DELAY_MS = 8000;
const MAX_DELAY_MS = 14000;
const LIFE_MS = 1200;
const TAIL_PX = 120;
/* Streak color: pure white RGB triplet (matches --color-cosmos-text-bright);
   composed with a per-frame alpha, which canvas gradients need inline. */
const STREAK_RGB = "255,255,255";

export function ShootingStars() {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
    if (reduced.matches) return;

    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    let w = 0;
    let h = 0;

    function resize() {
      w = canvas!.width = window.innerWidth * dpr;
      h = canvas!.height = window.innerHeight * dpr;
      canvas!.style.width = window.innerWidth + "px";
      canvas!.style.height = window.innerHeight + "px";
    }

    let streak: Streak | null = null;
    let spawnTimer: ReturnType<typeof setTimeout> | null = null;

    function spawn() {
      const angleDeg = -15 - Math.random() * 20;
      const angle = (angleDeg * Math.PI) / 180;
      const speedPxPerMs = (window.innerWidth * 1.1) / LIFE_MS;
      const startX = Math.random() * window.innerWidth * 0.4;
      const startY = Math.random() * window.innerHeight * 0.6;
      streak = {
        x: startX * dpr,
        y: startY * dpr,
        vx: Math.cos(angle) * speedPxPerMs * dpr,
        vy: Math.sin(angle) * speedPxPerMs * dpr,
        life: 0,
        maxLife: LIFE_MS,
        length: TAIL_PX * dpr,
      };
    }

    function scheduleNext() {
      const delay = MIN_DELAY_MS + Math.random() * (MAX_DELAY_MS - MIN_DELAY_MS);
      spawnTimer = setTimeout(() => {
        spawn();
        scheduleNext();
      }, delay);
    }

    let raf = 0;
    let last = performance.now();
    function frame(t: number) {
      const dt = t - last;
      last = t;
      ctx!.clearRect(0, 0, w, h);
      if (streak) {
        streak.life += dt;
        streak.x += streak.vx * dt;
        streak.y += streak.vy * dt;
        const progress = streak.life / streak.maxLife;
        const headAlpha = Math.min(1, 1 - Math.pow(progress, 3));

        // Zero-width window (mid-resize) spawns a zero-velocity streak;
        // hypot(0,0) division would feed NaN into createLinearGradient.
        const speed = Math.hypot(streak.vx, streak.vy);
        if (!Number.isFinite(speed) || speed === 0) {
          streak = null;
          raf = requestAnimationFrame(frame);
          return;
        }
        const tailX = streak.x - (streak.vx / speed) * streak.length;
        const tailY = streak.y - (streak.vy / speed) * streak.length;
        const grad = ctx!.createLinearGradient(streak.x, streak.y, tailX, tailY);
        grad.addColorStop(0, `rgba(${STREAK_RGB},${headAlpha})`);
        grad.addColorStop(1, `rgba(${STREAK_RGB},0)`);
        ctx!.strokeStyle = grad;
        ctx!.lineWidth = 2 * dpr;
        ctx!.lineCap = "round";
        ctx!.beginPath();
        ctx!.moveTo(streak.x, streak.y);
        ctx!.lineTo(tailX, tailY);
        ctx!.stroke();

        ctx!.fillStyle = `rgba(${STREAK_RGB},${headAlpha})`;
        ctx!.beginPath();
        ctx!.arc(streak.x, streak.y, 1.5 * dpr, 0, Math.PI * 2);
        ctx!.fill();

        if (streak.life >= streak.maxLife) streak = null;
      }
      raf = requestAnimationFrame(frame);
    }

    resize();
    window.addEventListener("resize", resize);
    raf = requestAnimationFrame(frame);
    scheduleNext();

    return () => {
      cancelAnimationFrame(raf);
      if (spawnTimer) clearTimeout(spawnTimer);
      window.removeEventListener("resize", resize);
    };
  }, []);

  return <canvas ref={canvasRef} className="page-canvas shooting-stars" aria-hidden />;
}
