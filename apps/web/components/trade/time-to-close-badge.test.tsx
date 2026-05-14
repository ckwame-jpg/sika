import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TimeToCloseBadge } from "@/components/trade/time-to-close-badge";

describe("TimeToCloseBadge", () => {
  it("renders nothing when minutes is null", () => {
    const { container } = render(<TimeToCloseBadge minutes={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders 'closing' when minutes is 0 or negative", () => {
    render(<TimeToCloseBadge minutes={0} />);
    expect(screen.getByText(/closing/i)).toBeInTheDocument();
  });

  it("renders T-Xm for short windows under 60 minutes", () => {
    render(<TimeToCloseBadge minutes={15} />);
    expect(screen.getByText(/T-15m/)).toBeInTheDocument();
  });

  it("applies urgent styling for minutes <= 30", () => {
    render(<TimeToCloseBadge minutes={20} />);
    expect(screen.getByText(/T-20m/)).toHaveClass("text-destructive");
  });

  it("does not apply urgent styling for minutes > 30", () => {
    render(<TimeToCloseBadge minutes={45} />);
    expect(screen.getByText(/T-45m/)).not.toHaveClass("text-destructive");
  });

  it("renders T-XhYm for windows between 1 and 24 hours", () => {
    render(<TimeToCloseBadge minutes={125} />);
    // 2h 5m
    expect(screen.getByText(/T-2h5m/)).toBeInTheDocument();
  });

  it("drops the minute suffix when the hour mark is exact", () => {
    render(<TimeToCloseBadge minutes={180} />);
    // 3h, no trailing 0m
    expect(screen.getByText(/T-3h/)).toBeInTheDocument();
    expect(screen.queryByText(/T-3h0m/)).not.toBeInTheDocument();
  });

  it("renders T-Xd for windows >= 24h", () => {
    render(<TimeToCloseBadge minutes={3060} />);
    // 2 days 3 hours = 3060 minutes → 2 days
    expect(screen.getByText(/T-2d/)).toBeInTheDocument();
  });
});
