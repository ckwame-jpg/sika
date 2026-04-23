"use client";

import { useEffect, useState } from "react";

const STORAGE_KEY = "sika_owner_admin_token";
const CHANGE_EVENT = "sika-admin-token-change";

export function useAdminToken() {
  const [token, setTokenState] = useState("");
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    const sync = () => setTokenState(window.localStorage.getItem(STORAGE_KEY) ?? "");
    sync();
    setLoaded(true);
    window.addEventListener("storage", sync);
    window.addEventListener(CHANGE_EVENT, sync);
    return () => {
      window.removeEventListener("storage", sync);
      window.removeEventListener(CHANGE_EVENT, sync);
    };
  }, []);

  const setToken = (value: string) => {
    const next = value.trim();
    setTokenState(next);
    if (!loaded) return;
    if (next) {
      window.localStorage.setItem(STORAGE_KEY, next);
    } else {
      window.localStorage.removeItem(STORAGE_KEY);
    }
    window.dispatchEvent(new Event(CHANGE_EVENT));
  };

  const clearToken = () => setToken("");

  return {
    token,
    loaded,
    hasToken: token.length > 0,
    setToken,
    clearToken,
  };
}
