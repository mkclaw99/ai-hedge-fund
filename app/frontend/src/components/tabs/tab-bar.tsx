import { Button } from '@/components/ui/button';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { FlowCreateDialog } from '@/components/panels/left/flow-create-dialog';
import { FlowEditDialog } from '@/components/panels/left/flow-edit-dialog';
import { useTabsContext } from '@/contexts/tabs-context';
import { useFlowManagementTabs } from '@/hooks/use-flow-management-tabs';
import { flowService } from '@/services/flow-service';
import { Flow } from '@/types/flow';
import { cn } from '@/lib/utils';
import { ChevronDown, Copy, FileText, FolderOpen, Layout, Pencil, Plus, Settings, Trash2, X } from 'lucide-react';
import { ReactNode, useState } from 'react';

interface TabBarProps {
  className?: string;
}

// Get icon for tab type
const getTabIcon = (type: string): ReactNode => {
  switch (type) {
    case 'flow':
      return <FileText size={13} />;
    case 'settings':
      return <Settings size={13} />;
    default:
      return <Layout size={13} />;
  }
};

export function TabBar({ className }: TabBarProps) {
  const { tabs, activeTabId, setActiveTab, closeTab, reorderTabs } = useTabsContext();
  const {
    flows,
    createDialogOpen,
    setCreateDialogOpen,
    handleCreateNewFlow,
    handleFlowCreated,
    handleOpenFlowInTab,
    handleDeleteFlow,
    handleRefresh,
  } = useFlowManagementTabs();
  const [draggedIndex, setDraggedIndex] = useState<number | null>(null);
  const [dragOverIndex, setDragOverIndex] = useState<number | null>(null);
  const [flowsOpen, setFlowsOpen] = useState(false);
  const [editingFlow, setEditingFlow] = useState<Flow | null>(null);

  const handleDuplicateFlow = async (flow: Flow) => {
    try {
      const copy = await flowService.duplicateFlow(flow.id);
      await handleRefresh();
      if (copy) await handleOpenFlowInTab(copy);
    } catch (error) {
      console.error('Failed to duplicate flow:', error);
    }
  };

  const handleDragStart = (e: React.DragEvent, index: number) => {
    setDraggedIndex(index);
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/html', ''); // Required for some browsers
  };

  const handleDragOver = (e: React.DragEvent, index: number) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    if (draggedIndex !== null && draggedIndex !== index) {
      setDragOverIndex(index);
    }
  };

  const handleDragLeave = () => setDragOverIndex(null);

  const handleDrop = (e: React.DragEvent, dropIndex: number) => {
    e.preventDefault();
    if (draggedIndex !== null && draggedIndex !== dropIndex) {
      reorderTabs(draggedIndex, dropIndex);
    }
    setDraggedIndex(null);
    setDragOverIndex(null);
  };

  const handleDragEnd = () => {
    setDraggedIndex(null);
    setDragOverIndex(null);
  };

  return (
    <div className={cn("flex items-stretch bg-panel border-b", className)}>
      {/* Scrollable tab strip */}
      <div className="flex items-center overflow-x-auto flex-1 min-w-0">
        {tabs.map((tab, index) => (
          <div
            key={tab.id}
            draggable
            onDragStart={(e) => handleDragStart(e, index)}
            onDragOver={(e) => handleDragOver(e, index)}
            onDragLeave={handleDragLeave}
            onDrop={(e) => handleDrop(e, index)}
            onDragEnd={handleDragEnd}
            className={cn(
              "group relative flex items-center gap-2 px-4 py-2.5 cursor-pointer transition-all duration-150 flex-shrink-0 max-w-52 select-none",
              activeTabId === tab.id
                ? "bg-panel before:absolute before:bottom-0 before:left-0 before:right-0 before:h-0.5 before:content-['']"
                : "bg-panel hover:bg-[var(--tab-hover-background)]",
              draggedIndex === index && "opacity-60 scale-[0.98]",
              dragOverIndex === index && "ring-1 ring-[var(--tab-accent)]/30",
              "hover:cursor-grab active:cursor-grabbing"
            )}
            style={{
              borderRight: `1px solid var(--tab-border)`,
              color: activeTabId === tab.id ? 'var(--tab-active-text)' : 'var(--tab-inactive-text)',
              backgroundColor: dragOverIndex === index ? 'var(--tab-hover-background)' : undefined,
            }}
            onMouseEnter={(e) => {
              if (activeTabId !== tab.id) e.currentTarget.style.color = 'var(--tab-hover-text)';
            }}
            onMouseLeave={(e) => {
              if (activeTabId !== tab.id) e.currentTarget.style.color = 'var(--tab-inactive-text)';
            }}
            onClick={() => setActiveTab(tab.id)}
          >
            {activeTabId === tab.id && (
              <div className="absolute bottom-0 left-0 right-0 h-0.5" style={{ backgroundColor: 'var(--tab-accent)' }} />
            )}

            <div
              className={cn("flex-shrink-0 transition-colors duration-150", activeTabId === tab.id ? "text-primary" : "")}
              style={{ color: activeTabId === tab.id ? 'var(--tab-icon-active)' : 'var(--tab-icon-inactive)' }}
            >
              {getTabIcon(tab.type)}
            </div>

            <span className="text-[13px] font-normal truncate min-w-0 transition-colors duration-150">
              {tab.title}
            </span>

            <Button
              variant="ghost"
              size="sm"
              className={cn(
                "h-5 w-5 p-0 flex-shrink-0 ml-1 rounded-sm transition-all duration-150",
                "opacity-0 group-hover:opacity-100 focus:opacity-100 focus:outline-none",
                activeTabId === tab.id && "opacity-70 hover:opacity-100"
              )}
              onMouseEnter={(e) => {
                e.currentTarget.style.backgroundColor = 'var(--tab-close-hover)';
                e.currentTarget.style.color = 'var(--tab-hover-text)';
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.backgroundColor = 'transparent';
                e.currentTarget.style.color = 'inherit';
              }}
              onClick={(e) => {
                e.stopPropagation();
                closeTab(tab.id);
              }}
              onMouseDown={(e) => e.stopPropagation()}
              title="Close tab"
            >
              <X size={11} className="transition-transform duration-150 hover:scale-110" />
            </Button>
          </div>
        ))}
      </div>

      {/* Pinned flow actions — replaces the old left Flows sidebar */}
      <div className="flex items-center flex-shrink-0 gap-0.5 px-1.5 border-l bg-panel">
        <Popover open={flowsOpen} onOpenChange={setFlowsOpen}>
          <PopoverTrigger asChild>
            <Button
              variant="ghost"
              size="sm"
              className="h-7 gap-1 text-xs text-muted-foreground hover:text-foreground"
              title="Open a saved flow"
            >
              <FolderOpen size={13} />
              Flows
              <ChevronDown size={12} />
            </Button>
          </PopoverTrigger>
          <PopoverContent align="end" className="w-64 p-1 max-h-80 overflow-auto">
            {(flows?.length ?? 0) === 0 ? (
              <div className="px-2 py-3 text-center text-xs text-muted-foreground">No saved flows yet</div>
            ) : (
              flows.map((flow) => (
                <div
                  key={flow.id}
                  role="button"
                  className="group flex items-center justify-between gap-2 rounded-sm px-2 py-1.5 text-sm cursor-pointer hover:bg-accent"
                  onClick={() => {
                    handleOpenFlowInTab(flow);
                    setFlowsOpen(false);
                  }}
                >
                  <span className="truncate">{flow.name}</span>
                  <div className="flex items-center flex-shrink-0 gap-0.5 opacity-0 group-hover:opacity-100">
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-5 w-5 text-muted-foreground hover:text-foreground"
                      onClick={(e) => {
                        e.stopPropagation();
                        setEditingFlow(flow);
                        setFlowsOpen(false);
                      }}
                      title={`Rename "${flow.name}"`}
                    >
                      <Pencil size={12} />
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-5 w-5 text-muted-foreground hover:text-foreground"
                      onClick={(e) => {
                        e.stopPropagation();
                        setFlowsOpen(false);
                        handleDuplicateFlow(flow);
                      }}
                      title={`Duplicate "${flow.name}"`}
                    >
                      <Copy size={12} />
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-5 w-5 text-muted-foreground hover:text-red-500"
                      onClick={(e) => {
                        e.stopPropagation();
                        handleDeleteFlow(flow);
                      }}
                      title={`Delete "${flow.name}"`}
                    >
                      <Trash2 size={12} />
                    </Button>
                  </div>
                </div>
              ))
            )}
          </PopoverContent>
        </Popover>

        <Button
          variant="ghost"
          size="sm"
          className="h-7 gap-1 text-xs text-muted-foreground hover:text-foreground"
          onClick={handleCreateNewFlow}
          title="Create a new flow"
        >
          <Plus size={14} />
          New Flow
        </Button>
      </div>

      <FlowCreateDialog
        isOpen={createDialogOpen}
        onClose={() => setCreateDialogOpen(false)}
        onFlowCreated={handleFlowCreated}
      />

      <FlowEditDialog
        flow={editingFlow}
        isOpen={!!editingFlow}
        onClose={() => setEditingFlow(null)}
        onFlowUpdated={handleRefresh}
      />
    </div>
  );
}
