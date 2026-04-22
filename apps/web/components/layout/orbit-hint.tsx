"use client";

import { useEffect, useState } from "react";

export function OrbitHint() {
  const [hide, setHide] = useState(false);

  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setHide(true);
      return;
    }
    const t = window.setTimeout(() => setHide(true), 6000);
    return () => window.clearTimeout(t);
  }, []);

  return (
    <div id="orbitHint" className={`orbit-hint${hide ? " hide" : ""}`} aria-hidden>
      drag the sky
    </div>
  );
}
