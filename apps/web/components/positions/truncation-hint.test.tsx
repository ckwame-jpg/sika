// Bug #28 follow-up: tests for the truncation hint banner.

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TruncationHint } from "./truncation-hint";

describe("TruncationHint", () => {
  it("renders the visible count", () => {
    render(<TruncationHint visibleCount={200} limitParam="paper_limit" />);

    expect(screen.getByText(/200 most recent/)).toBeInTheDocument();
  });

  it("names the query parameter the operator should bump", () => {
    render(<TruncationHint visibleCount={200} limitParam="demo_limit" />);

    expect(screen.getByText("demo_limit")).toBeInTheDocument();
  });

  it("uses role='note' so screen readers announce it as a notice", () => {
    render(<TruncationHint visibleCount={200} limitParam="paper_limit" />);

    expect(screen.getByRole("note")).toBeInTheDocument();
  });
});
