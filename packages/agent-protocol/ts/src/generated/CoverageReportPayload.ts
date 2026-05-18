/**
 * Payload of a coverage-report card. Summarises mastery of a knowledge graph: section-level coverage / mastery and a list of weak knowledge points the learner should remediate. The actions block tells the UI whether a 'practice weak points' button should be offered.
 */
export interface CoverageReportPayload {
  reportId: string;
  title: string;
  /**
   * 0..1 fraction of nodes covered.
   */
  overallCoverage: number;
  /**
   * 0..1 fraction of nodes mastered.
   */
  overallMastery: number;
  summary?: string | null;
  sections: {
    id: string;
    name: string;
    coverage: number;
    mastery: number;
    totalCount: number;
    learnedCount: number;
    testedCount: number;
    masteredCount: number;
    weakKpIds: string[];
    [k: string]: any;
  }[];
  weakPoints: {
    id: string;
    name: string;
    sectionName?: string | null;
    accuracy: number;
    recommendation: string;
    [k: string]: any;
  }[];
  actions: {
    allowRemediateQuiz: boolean;
    remediateActionLabel: string;
    [k: string]: any;
  };
  [k: string]: any;
}
