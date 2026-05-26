import { Layout } from './components/layout';
import { Toaster } from './components/ui/sonner';
import { TooltipProvider } from './components/ui/tooltip';

export default function App() {
  return (
    <>
      <TooltipProvider delayDuration={300}>
        <Layout />
      </TooltipProvider>
      <Toaster />
    </>
  );
}
