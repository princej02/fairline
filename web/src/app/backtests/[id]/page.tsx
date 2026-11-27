import { Suspense } from "react";
import { BacktestView } from "@/components/BacktestView";
import { Loading } from "@/components/ui";

export default function Page() {
  return (
    <Suspense fallback={<Loading />}>
      <BacktestView />
    </Suspense>
  );
}
