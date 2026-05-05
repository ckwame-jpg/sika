import * as React from "react";
import { cn } from "@/lib/utils";

interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  mono?: boolean;
}

export const Input = React.forwardRef<HTMLInputElement, InputProps>(
  ({ className, type, mono, ...props }, ref) => {
    return (
      <input
        type={type}
        ref={ref}
        className={cn(
          "flex h-8 w-full rounded border border-border bg-surface px-3 text-sm",
          "text-foreground placeholder:text-muted-foreground",
          "hover:border-border-bright hover:bg-surface-hover",
          "transition-colors duration-[120ms]",
          "focus-visible:ring-focus",
          "disabled:opacity-40 disabled:cursor-not-allowed",
          mono && "font-mono",
          className,
        )}
        {...props}
      />
    );
  },
);
Input.displayName = "Input";
