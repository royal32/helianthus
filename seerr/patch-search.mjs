import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';

const path = '/app/dist/routes/search.js';
const expectedHash =
  '2c9be9ee223309799adec5f4d1d1a0e4d9ae53c368896038c277488131c8caa4';
const source = readFileSync(path, 'utf8');
const sourceHash = createHash('sha256').update(source).digest('hex');

if (sourceHash !== expectedHash) {
  throw new Error(
    `Refusing to patch unexpected Seerr search route: ${sourceHash}`
  );
}

const target = `        const media = await Media_1.default.getRelatedMedia(req.user, results.results.map((result) => ({
            tmdbId: result.id,
            mediaType: result.media_type,
        })));
        return res.status(200).json({
            page: results.page,
            totalPages: results.total_pages,
            totalResults: results.total_results,
            results: (0, Search_1.mapSearchResults)(results.results, media),
        });`;

const replacement = `        const mediaResults = results.results.filter((result) => result.media_type !== 'person');
        const media = await Media_1.default.getRelatedMedia(req.user, mediaResults.map((result) => ({
            tmdbId: result.id,
            mediaType: result.media_type,
        })));
        return res.status(200).json({
            page: results.page,
            totalPages: results.total_pages,
            totalResults: results.total_results,
            results: (0, Search_1.mapSearchResults)(mediaResults, media),
        });`;

if (!source.includes(target)) {
  throw new Error('Seerr search-route patch target was not found');
}

writeFileSync(path, source.replace(target, replacement));
