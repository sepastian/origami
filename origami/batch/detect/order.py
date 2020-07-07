#!/usr/bin/env python3

import click
import collections
import logging
import shapely.ops

from pathlib import Path
from itertools import chain

from origami.batch.core.processor import Processor
from origami.batch.core.io import Artifact, Stage, Input, Output
from origami.batch.core.lines import reliable_contours
from origami.core.xycut import polygon_order
from origami.core.separate import Separators, ObstacleSampler


class RegionsFilter:
	def __init__(self, spec):
		self._paths = set()
		for s in spec.split(","):
			self._paths.add(
				tuple(s.strip().split("/")))

	def __call__(self, path):
		return tuple(path[:2]) in self._paths


class Combinator:
	def __init__(self, paths):
		mapping = collections.defaultdict(list)
		for k in paths:
			parts = k[-1].split(".")
			if len(parts) > 1:
				mapping[k[:-1] + (parts[0], )].append(k)
			else:
				mapping[k].append(k)
		self._mapping = mapping

	def contours(self, contours):
		combined = dict()
		for k, v in self._mapping.items():
			if len(v) == 1:
				combined[k] = contours[v[0]]
			else:
				geom = shapely.ops.cascaded_union([
					contours[x] for x in v])
				if geom.geom_type != "Polygon":
					geom = geom.convex_hull
				combined[k] = geom
		return combined

	def lines(self, lines):
		lines_by_block = collections.defaultdict(list)
		for k, line in lines.items():
			lines_by_block[k[:3]].append(line)

		combined = dict()
		for k, v in self._mapping.items():
			combined[k] = list(chain(
				*[lines_by_block[x] for x in v]))

		new_lines = dict()
		for k, v in combined.items():
			for i, line in enumerate(v):
				new_lines[k + (1 + i,)] = line

		return new_lines


class ReadingOrderProcessor(Processor):
	def __init__(self, options):
		super().__init__(options)
		self._options = options
		self._ignore = RegionsFilter(options["ignore"])

	@property
	def processor_name(self):
		return __loader__.name

	def compute_order(self, page, contours, sampler):
		fringe = page.geometry(dewarped=True).rel_length(self._options["fringe"])
		return polygon_order(contours, fringe=fringe, score=sampler)

	def xycut_orders(self, page, contours, separators):
		contours = dict((k, v) for k, v in contours.items() if not v.is_empty)

		by_labels = collections.defaultdict(list)
		for p, contour in contours.items():
			by_labels[p[:2]].append((p, contour))

		by_labels[("*",)] = [
			(k, v) for k, v in contours.items() if not self._ignore(k)]

		sampler = ObstacleSampler(separators)

		return dict(
			(p, self.compute_order(page, v, sampler))
			for p, v in by_labels.items())

	def artifacts(self):
		return [
			("warped", Input(Artifact.SEGMENTATION, stage=Stage.WARPED)),
			("dewarped", Input(Artifact.CONTOURS, stage=Stage.DEWARPED)),
			("aggregate", Input(Artifact.CONTOURS, Artifact.LINES, stage=Stage.AGGREGATE)),
			("output", Output(Artifact.ORDER, Artifact.CONTOURS, stage=Stage.RELIABLE)),
		]

	def process(self, page_path: Path, warped, dewarped, aggregate, output):
		blocks = aggregate.blocks
		if not blocks:
			return

		page = list(blocks.values())[0].page

		combinator = Combinator(blocks.keys())
		contours = combinator.contours(dict(
			(k, v.image_space_polygon) for k, v in blocks.items()))
		lines = combinator.lines(aggregate.lines)
		reliable = reliable_contours(contours, lines)

		min_area = page.geometry(True).rel_area(self._options["region_area"])
		reliable = dict(
			(k, v)
			for k, v in reliable.items()
			if v.area >= min_area)

		separators = Separators(
			warped.segmentation, dewarped.separators)

		with output.contours(copy_meta_from=aggregate) as zf:
			for k, contour in reliable.items():
				if contour.geom_type != "Polygon" and not contour.is_empty:
					logging.error(
						"reliable contour %s is %s" % (k, contour.geom_type))
				zf.writestr("/".join(k) + ".wkt", contour.wkt)

		orders = self.xycut_orders(page, reliable, separators)

		orders = dict(("/".join(k), [
			"/".join(p) for p in ps]) for k, ps in orders.items())

		output.order(dict(
			version=1,
			orders=orders))


@click.command()
@click.argument(
	'data_path',
	type=click.Path(exists=True),
	required=True)
@click.option(
	'--ignore',
	type=str,
	default="regions/ILLUSTRATION")
@click.option(
	'--fringe',
	type=float,
	default=0.001)
@click.option(
	'--region-area',
	type=float,
	default=0.01,
	help="Ignore regions below this relative size.")
@Processor.options
def reading_order(data_path, **kwargs):
	""" Detect reading order on all document images in DATA_PATH. """
	processor = ReadingOrderProcessor(kwargs)
	processor.traverse(data_path)


if __name__ == "__main__":
	reading_order()

