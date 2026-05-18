/**
 * Payload of an orchestration-plan card emitted by the Coordinator agent. Lists every worker task (id + agent + prompt + dependencies). The UI renders one row per task with a live status dot driven by sibling assistant messages tagged with the same orchestrationGroupId + orchestrationTaskId.
 */
export interface OrchestrationPlanPayload {
  /**
   * Short prose explanation of why the coordinator picked this plan shape.
   */
  rationale?: string;
  /**
   * How the workers run with respect to each other.
   */
  mode?: "parallel" | "sequential" | "dag";
  tasks: {
    /**
     * Stable id for this task within the orchestration group.
     */
    id: string;
    /**
     * Which agent runs this task.
     */
    agentId: string;
    /**
     * Per-task prompt the coordinator drafted.
     */
    prompt?: string;
    /**
     * Task ids that must finish before this one starts.
     */
    dependsOn?: string[];
    /**
     * Task ids whose outputs this task is allowed to read.
     */
    readOutputsFrom?: string[];
    [k: string]: any;
  }[];
  coordinator?: {
    agentId?: string;
    name?: string;
    avatar?: string;
    [k: string]: any;
  };
  [k: string]: any;
}
