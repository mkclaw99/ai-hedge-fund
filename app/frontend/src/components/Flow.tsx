import {
  Background,
  BackgroundVariant,
  ColorMode,
  Connection,
  Edge,
  EdgeChange,
  MarkerType,
  NodeChange,
  Panel,
  ReactFlow,
  addEdge,
  useEdgesState,
  useNodesState
} from '@xyflow/react';
import { Redo2, Undo2 } from 'lucide-react';
import { useTheme } from 'next-themes';
import { useCallback, useEffect, useRef, useState } from 'react';

import '@xyflow/react/dist/style.css';

import { useFlowContext } from '@/contexts/flow-context';
import { useEnhancedFlowActions } from '@/hooks/use-enhanced-flow-actions';
import { useFlowHistory } from '@/hooks/use-flow-history';
import { useFlowKeyboardShortcuts, useKeyboardShortcuts } from '@/hooks/use-keyboard-shortcuts';
import { useToastManager } from '@/hooks/use-toast-manager';
import { AppNode } from '@/nodes/types';
import { edgeTypes } from '../edges';
import { nodeTypes } from '../nodes';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from './ui/tooltip';

type FlowProps = {
  className?: string;
};

export function Flow({ className = '' }: FlowProps) {
  const { theme, resolvedTheme } = useTheme();
  
  // Use the resolved theme for ReactFlow ColorMode
  const colorMode: ColorMode = resolvedTheme === 'light' ? 'light' : 'dark';
  
  const [nodes, setNodes, onNodesChange] = useNodesState<AppNode>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [isInitialized, setIsInitialized] = useState(false);
  const proOptions = { hideAttribution: true };
  
  // Get flow context for flow ID
  const { currentFlowId } = useFlowContext();
  
  // Get enhanced flow actions for complete state persistence
  const { saveCurrentFlowWithCompleteState } = useEnhancedFlowActions();
  
  // Get toast manager
  const { success, error } = useToastManager();

  // Initialize flow history (each flow maintains its own separate history)
  const { takeSnapshot, undo, redo, canUndo, canRedo, clearHistory } = useFlowHistory({ flowId: currentFlowId });

  // Create debounced auto-save function
  const autoSaveTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const lastSavedFlowIdRef = useRef<number | null>(null);
  
  const autoSave = useCallback(async (flowIdToSave?: number | null) => {
    // Use the provided flowId or fall back to current flow ID
    const targetFlowId = flowIdToSave !== undefined ? flowIdToSave : currentFlowId;
    
    // Clear any existing timeout
    if (autoSaveTimeoutRef.current) {
      clearTimeout(autoSaveTimeoutRef.current);
    }
    
    // Set new timeout for debounced save
    autoSaveTimeoutRef.current = setTimeout(async () => {
      // Double-check that we're still saving to the correct flow
      if (!targetFlowId) {
        return;
      }
      
      // If the current flow has changed since this auto-save was scheduled, skip it
      if (targetFlowId !== currentFlowId) {
        return;
      }
      
      try {
        await saveCurrentFlowWithCompleteState();
        lastSavedFlowIdRef.current = targetFlowId;
      } catch (error) {
        console.error(`[Auto-save] Failed to save flow ${targetFlowId}:`, error);
      }
    }, 1000); // 1 second debounce
  }, [currentFlowId, saveCurrentFlowWithCompleteState]);

  // Enhanced onNodesChange handler with auto-save for specific change types
  const handleNodesChange = useCallback((changes: NodeChange<AppNode>[]) => {
    // Apply the changes first
    onNodesChange(changes);
    
    // Check if any of the changes should trigger auto-save
    const shouldAutoSave = changes.some(change => {
      switch (change.type) {
        case 'add':
          return true;
        case 'remove':
          return true;
        case 'position':
          // Only auto-save position changes when dragging is complete
          if (!change.dragging) {
            return true;
          }
          return false;
        default:
          return false;
      }
    });

    // Trigger auto-save if needed and flow is initialized
    // IMPORTANT: Capture the current flow ID at the time of the change
    if (shouldAutoSave && isInitialized && currentFlowId) {
      const flowIdAtTimeOfChange = currentFlowId;
      autoSave(flowIdAtTimeOfChange);
    }
  }, [onNodesChange, autoSave, isInitialized, currentFlowId]);

  // Enhanced onEdgesChange handler with auto-save for edge removal
  const handleEdgesChange = useCallback((changes: EdgeChange[]) => {
    // Apply the changes first
    onEdgesChange(changes);
    
    // Check if any of the changes should trigger auto-save
    const shouldAutoSave = changes.some(change => {
      switch (change.type) {
        case 'remove':
          return true;
        default:
          return false;
      }
    });

    // Trigger auto-save if needed and flow is initialized
    // IMPORTANT: Capture the current flow ID at the time of the change
    if (shouldAutoSave && isInitialized && currentFlowId) {
      const flowIdAtTimeOfChange = currentFlowId;
      autoSave(flowIdAtTimeOfChange);
    }
  }, [onEdgesChange, autoSave, isInitialized, currentFlowId]);

  // Cleanup timeout on unmount
  useEffect(() => {
    return () => {
      if (autoSaveTimeoutRef.current) {
        clearTimeout(autoSaveTimeoutRef.current);
      }
    };
  }, []);

  // Cancel pending auto-saves when flow changes to prevent cross-flow saves
  useEffect(() => {
    if (autoSaveTimeoutRef.current) {
      clearTimeout(autoSaveTimeoutRef.current);
      autoSaveTimeoutRef.current = null;
    }
  }, [currentFlowId]);

  // Take an initial empty-canvas baseline ONLY for a brand-new (unsaved) flow, so the
  // first edits can be undone back to empty. For a saved flow being opened, the loaded
  // state is the baseline (captured by the change effect below) — we must NOT snapshot
  // the transient empty canvas first, or "Undo" right after opening would wipe the
  // freshly-loaded flow instead of reverting a user action.
  useEffect(() => {
    if (isInitialized && currentFlowId == null && nodes.length === 0 && edges.length === 0) {
      takeSnapshot();
    }
  }, [isInitialized, currentFlowId, takeSnapshot, nodes.length, edges.length]);

  // Take snapshot when nodes or edges change (debounced)
  useEffect(() => {
    if (!isInitialized) return;
    
    const timeoutId = setTimeout(() => {
      takeSnapshot();
    }, 500); // Debounce snapshots by 500ms

    return () => clearTimeout(timeoutId);
  }, [nodes, edges, takeSnapshot, isInitialized]);

  // // Auto-save when nodes or edges change (debounced with longer delay)
  // useEffect(() => {
  //   if (!isInitialized) return;
    
  //   const timeoutId = setTimeout(async () => {
  //     try {
  //       await saveCurrentFlowWithCompleteState();
  //       // Don't show success toast for auto-save to avoid spam
  //     } catch (err) {
  //       // Only show error notifications for auto-save failures
  //       error('Auto-save failed', 'auto-save-error');
  //     }
  //   }, 1000); // Debounce auto-save by 1 second (longer than undo/redo)

  //   return () => clearTimeout(timeoutId);
  // }, [nodes, edges, saveCurrentFlowWithCompleteState, error, isInitialized]);

  // Connect keyboard shortcuts to save flow with toast
  useFlowKeyboardShortcuts(async () => {
    try {
      const savedFlow = await saveCurrentFlowWithCompleteState();
      if (savedFlow) {
        success(`"${savedFlow.name}" saved!`, 'flow-save');
      } else {
        error('Failed to save flow', 'flow-save-error');
      }
    } catch (err) {
      error('Failed to save flow', 'flow-save-error');
    }
  });

  // Undo/redo restore state via setNodes/setEdges (which don't go through the change
  // handlers), so trigger an auto-save afterwards — otherwise the reverted state isn't
  // persisted and would be lost on reload (the original edit already auto-saved).
  const handleUndo = useCallback(() => { undo(); autoSave(); }, [undo, autoSave]);
  const handleRedo = useCallback(() => { redo(); autoSave(); }, [redo, autoSave]);

  // Add undo/redo keyboard shortcuts
  useKeyboardShortcuts({
    shortcuts: [
      {
        key: 'z',
        ctrlKey: true,
        metaKey: true,
        callback: handleUndo,
        preventDefault: true,
      },
      {
        key: 'z',
        ctrlKey: true,
        metaKey: true,
        shiftKey: true,
        callback: handleRedo,
        preventDefault: true,
      },
    ],
  });
  
  // Initialize the flow when it first renders
  const onInit = useCallback(() => {
    if (!isInitialized) {
      setIsInitialized(true);
    }
  }, [isInitialized]);

  // Connect two nodes with marker
  const onConnect = useCallback(
    (connection: Connection) => {
      // Create a new edge with a marker and unique ID
      const newEdge: Edge = {
        ...connection,
        id: `edge-${Date.now()}`, // Add unique ID
        markerEnd: {
          type: MarkerType.ArrowClosed,
        },
      };
      setEdges((eds) => addEdge(newEdge, eds));
      
      // Auto-save new connections immediately (structural change)
      if (currentFlowId) {
        // IMPORTANT: Capture the current flow ID at the time of the change
        const flowIdAtTimeOfChange = currentFlowId;
        
        // Clear any pending debounced saves and save immediately
        if (autoSaveTimeoutRef.current) {
          clearTimeout(autoSaveTimeoutRef.current);
        }
        
        // Use setTimeout to ensure the edge is added to state first
        setTimeout(async () => {
          // Double-check that we're still saving to the correct flow
          if (flowIdAtTimeOfChange !== currentFlowId) {
            return;
          }
          
          try {
            await saveCurrentFlowWithCompleteState();
          } catch (error) {
            console.error(`[Auto-save] Failed to save new connection for flow ${flowIdAtTimeOfChange}:`, error);
          }
        }, 100);
      }
    },
    [setEdges, currentFlowId, saveCurrentFlowWithCompleteState]
  );

  // Theme-aware background colors
  const backgroundStyle = {
    backgroundColor: 'hsl(var(--background))'
  };
  
  const gridColor = resolvedTheme === 'light' ? 'hsl(var(--foreground))' : 'hsl(var(--muted-foreground))';

  return (
    <div className={`w-full h-full ${className}`}>
      <TooltipProvider>
        <ReactFlow
          nodes={nodes}
          nodeTypes={nodeTypes}
          onNodesChange={handleNodesChange}
          edges={edges}
          edgeTypes={edgeTypes}
          onEdgesChange={handleEdgesChange}
          onConnect={onConnect}
          onInit={onInit}
          colorMode={colorMode}
          proOptions={proOptions}
        >
          <Background 
            variant={BackgroundVariant.Dots}
            gap={13}
            color={gridColor}
            style={backgroundStyle}
          />
          {/* Undo / Redo controls (history lives in useFlowHistory, per flow) */}
          <Panel position="top-left" className="m-2">
            <div className="flex items-center gap-1 rounded-md border border-border bg-node p-1 shadow-md">
              <Tooltip delayDuration={300}>
                <TooltipTrigger asChild>
                  <span tabIndex={-1} className="inline-flex">
                    <button
                      type="button"
                      onClick={handleUndo}
                      disabled={!canUndo}
                      aria-label="Undo last step"
                      className="nodrag inline-flex h-8 w-8 items-center justify-center rounded text-primary transition-colors hover:bg-accent disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent"
                    >
                      <Undo2 className="h-4 w-4" />
                    </button>
                  </span>
                </TooltipTrigger>
                <TooltipContent side="bottom">
                  {canUndo ? 'Undo last step (⌘Z)' : 'Nothing to undo'}
                </TooltipContent>
              </Tooltip>
              <Tooltip delayDuration={300}>
                <TooltipTrigger asChild>
                  <span tabIndex={-1} className="inline-flex">
                    <button
                      type="button"
                      onClick={handleRedo}
                      disabled={!canRedo}
                      aria-label="Redo"
                      className="nodrag inline-flex h-8 w-8 items-center justify-center rounded text-primary transition-colors hover:bg-accent disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent"
                    >
                      <Redo2 className="h-4 w-4" />
                    </button>
                  </span>
                </TooltipTrigger>
                <TooltipContent side="bottom">
                  {canRedo ? 'Redo (⇧⌘Z)' : 'Nothing to redo'}
                </TooltipContent>
              </Tooltip>
            </div>
          </Panel>
        </ReactFlow>
      </TooltipProvider>
    </div>
  );
} 