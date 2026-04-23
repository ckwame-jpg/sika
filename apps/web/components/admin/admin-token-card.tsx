"use client";

import { FormEvent, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";

interface AdminTokenCardProps {
  title?: string;
  description?: string;
  onSubmit: (token: string) => void;
}

export function AdminTokenCard({
  title = "Owner Access",
  description = "Enter the owner admin token to view live account and auto-trading data.",
  onSubmit,
}: AdminTokenCardProps) {
  const [value, setValue] = useState("");

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    onSubmit(value);
  };

  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>{title}</CardTitle>
          <CardDescription>{description}</CardDescription>
        </div>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit} className="flex flex-col gap-2 sm:flex-row">
          <Input
            type="password"
            value={value}
            onChange={(event) => setValue(event.target.value)}
            placeholder="X-Sika-Admin-Token"
            autoComplete="off"
            className="min-w-0 flex-1"
          />
          <Button type="submit" variant="primary" size="md">
            Unlock
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
