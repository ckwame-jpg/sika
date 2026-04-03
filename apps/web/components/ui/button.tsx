"use client";

import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  [
    "inline-flex items-center justify-center gap-1.5 whitespace-nowrap",
    "font-sans text-sm font-medium",
    "rounded transition-all duration-[120ms]",
    "focus-visible:ring-focus",
    "disabled:pointer-events-none disabled:opacity-40",
    "select-none cursor-pointer",
  ],
  {
    variants: {
      variant: {
        primary: [
          "bg-accent text-white",
          "hover:bg-accent/90 active:bg-accent/80",
          "shadow-sm",
        ],
        secondary: [
          "bg-surface border border-border text-foreground",
          "hover:bg-surface-hover hover:border-border-bright",
          "active:scale-[0.98]",
        ],
        ghost: [
          "text-muted-foreground",
          "hover:bg-surface-hover hover:text-foreground",
          "active:bg-surface",
        ],
        danger: [
          "bg-negative/10 text-negative border border-negative/20",
          "hover:bg-negative/20 hover:border-negative/40",
        ],
        positive: [
          "bg-positive/10 text-positive border border-positive/20",
          "hover:bg-positive/20 hover:border-positive/40",
        ],
        link: [
          "text-accent underline-offset-4",
          "hover:underline p-0 h-auto",
        ],
      },
      size: {
        xs: "h-6 px-2 text-xs rounded",
        sm: "h-7 px-2.5 text-xs",
        md: "h-8 px-3 text-sm",
        lg: "h-9 px-4 text-sm",
        icon: "h-7 w-7 p-0",
        "icon-sm": "h-6 w-6 p-0",
      },
    },
    defaultVariants: {
      variant: "secondary",
      size: "md",
    },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return (
      <Comp
        ref={ref}
        className={cn(buttonVariants({ variant, size }), className)}
        {...props}
      />
    );
  },
);
Button.displayName = "Button";

export { buttonVariants };
