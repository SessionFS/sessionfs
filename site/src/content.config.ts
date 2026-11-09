import { defineCollection } from 'astro:content';
import { z } from 'astro/zod';
import { docsLoader } from '@astrojs/starlight/loaders';
import { docsSchema } from '@astrojs/starlight/schema';

export const collections = {
	docs: defineCollection({
		loader: docsLoader(),
		schema: docsSchema({
			extend: z.object({
				// Which plan feature a page describes and where it runs. Read by the
				// session helper's docs index; not rendered on the page.
				availability: z
					.object({
						feature: z.string().optional(),
						deployment: z.enum(['cloud', 'self-hosted', 'both']).default('both'),
					})
					.optional(),
			}),
		}),
	}),
};
