import type { Preview } from '@storybook/react';
import './preview.css';

const preview: Preview = {
  parameters: {
    backgrounds: {
      default: 'light',
      values: [
        { name: 'light', value: '#ffffff' },
        { name: 'muted', value: '#f4f4f5' },
        { name: 'dark', value: '#09090b' },
      ],
    },
    controls: {
      matchers: {
        color: /(background|color)$/i,
        date: /Date$/i,
      },
    },
    a11y: {
      // axe-core options; results render in the a11y panel.
      config: {},
      options: {},
      manual: false,
    },
    docs: {
      toc: true,
    },
  },
  tags: ['autodocs'],
};

export default preview;
