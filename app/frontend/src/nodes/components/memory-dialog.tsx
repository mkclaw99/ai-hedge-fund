import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { FlowMemory } from '@/services/memory-api';
import { MemoryView } from './memory-view';

interface MemoryDialogProps {
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
  memory: FlowMemory | null;
  loading: boolean;
}

export function MemoryDialog({ isOpen, onOpenChange, memory, loading }: MemoryDialogProps) {
  return (
    <Dialog open={isOpen} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-4xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Flow Research Memory</DialogTitle>
          <DialogDescription>
            What this flow has learned across runs. Each analyst reads back only its own
            prior calls; the Portfolio Manager reads everything here.
          </DialogDescription>
        </DialogHeader>

        <MemoryView memory={memory} loading={loading} />
      </DialogContent>
    </Dialog>
  );
}
