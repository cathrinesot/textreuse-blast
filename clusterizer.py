import os, sys, json, gzip, tarfile, argparse, shelve
import networkx as nx
from operator import itemgetter
from natsort import natsorted
from joblib import Parallel, delayed
from collections import defaultdict

from community import CommunityDetector
from text_logging import get_logger
class ParallelJobRunner:

	def __init__(self, output_folder, min_length, max_length, node_similarity, alignment_ranges, compress):
		self.output_folder = output_folder
		self.min_length = min_length
		self.max_length = max_length
		self.alignment_ranges = alignment_ranges
		self.node_similarity = node_similarity

		# Read data, can be either tsv files or gzipped tar files
	def read_data_parallel(self, filename, file_index, min_alignment_score):
		file_loc = self.output_folder + "/batches/" + filename
		data = {}
		if filename.endswith("tar.gz"): ## TAR compressed
			try:
				tarf = tarfile.open(file_loc)
			except tarfile.ReadError:
				print("Error reading file {}, skipping".format(filename))
				return data
			members = tarf.getmembers()
			for member in members:
				if not member.name.endswith(".tsv"): continue
				memberfile = tarf.extractfile(member)
				tsv_data = self.process_tsv(memberfile.read().decode(), min_alignment_score)
				data.update(tsv_data)
		elif filename.endswith(".gz"):
			with gzip.open(file_loc, "rt") as gzf:
				gd = json.loads(gzf.read())
			for key, value in gd.items():
				tsv_data = self.process_tsv(value, min_alignment_score)
				data.update(tsv_data)
		elif filename.endswith(".tsv"):
			with open(file_loc, "r") as tsv_file:
				tsv_data = self.process_tsv(tsv_file.read(), min_alignment_score)
				data.update(tsv_data)
		else:
			pass
			#print("Skipping file/folder: {}".format(filename))
		#	raise TypeError("Wrong file format.")
		return data

	def read_data_parallel_iterations(self, filename, file_index, min_alignment_score, file_style):
		data = {}
		if file_style == "clustered":
			with gzip.open(filename, "rt") as gzf:
				gd = json.loads(gzf.read())
			for key in sorted(list(gd.keys())):
				nodes = gd[key][0]
				start_node = nodes.pop(0)
				start_node_doc = start_node.split("___")[0]
				data[start_node_doc] = data.get(start_node_doc, [])
				for node in nodes:
					data[start_node_doc].append([start_node, node])
		else:
			data = self.read_data_parallel(filename, file_index, min_alignment_score)
		return (data, file_index)

		## Read the actual TSV file
	def process_tsv(self, data, min_alignment_score):
		tsv_data = {}
		for line in data.split("\n"):
			if line.startswith("# Query:"): ## Starting a new query
				key = line.split(" ", 3)[3].strip()
				continue
			elif not line or line.startswith("#"): # other #'s are just comments
				continue
			tabs = line.split("\t")
			other_key = tabs[0]
			q_start, q_end, h_start, h_end, length = [int(val) for val in tabs[1:-1]]
			alignment = float(tabs[-1])
			if key == other_key or not self.min_length <= length <= self.max_length or alignment < 100*min_alignment_score:
				continue
			if self.alignment_ranges:
				skip = False
				for alignment_range in self.alignment_ranges.split(";"):
					start,score,end = alignment_range.split(",")
					if int(start) <= length < int(end) and alignment < 100*float(score):
						skip = True
				if skip:
					continue
			tsv_data.setdefault(key, [])
			tsv_data[key].append([q_start, q_end, h_start, h_end, length, other_key])
		return tsv_data

		## Flattens data, returns all hsps from the subkeys under the main key (e.g. x_1_2, x_3_4 --> x)
	def flatten_data_parallel(self, key, value):
		flattened_data = {}
		real_hsps = []
		for sub_key_data in value:
			sub_key = sub_key_data[0]
			query_index_start = int(sub_key.split("___")[0].split("__")[-1].split("_")[0])
			query_extra = 0
			if query_index_start != 0: ## To get actual offset values
				query_extra = query_index_start

			for hsp in sub_key_data[1]:
				q_start, q_end, h_start, h_end, length, other_key = hsp
				hit_index_start = int(other_key.split("___")[0].split("__")[-1].split("_")[0])
				hit_extra = 0
				if hit_index_start != 0: ## Same here
					hit_extra = hit_index_start

				## Adding the offsets to the values
				q_start += query_extra
				q_end += query_extra
				h_start += hit_extra
				h_end += hit_extra
				other_key = other_key.split("__")[0]
				real_hsps.append([q_start, q_end, h_start, h_end, length, other_key])
		flattened_data[key] = real_hsps
		return flattened_data

	def find_nodes_parallel(self, key, value, itern):
		nodes = {}
		for hsp in value:
			if type(hsp[0]) == str:
				new_hsp = []
				new_hsp += [int(v) for v in hsp[0].split("___")[1].split("_")]
				new_hsp += [int(v) for v in hsp[1].split("___")[1].split("_")]
				new_hsp.append(None) ## Skip length
				new_hsp.append(hsp[1].split("___")[0])
				hsp = new_hsp
			begin_node = hsp[0:2]
			end_node = hsp[2:4]
			other_key = hsp[5]
			nodes.setdefault(key, []).append(begin_node)
			nodes.setdefault(other_key, []).append(end_node)
		return (nodes, itern)

	def stringify(self, key, node):
		return "{}___{}_{}".format(key, node[0], node[1])

	def stringify_data_parallel(self, key, value, itern):
		data = {}
		data[key] = []
		for hsp in value:
			begin = self.stringify(key, hsp[0:2])
			end = self.stringify(hsp[5], hsp[2:4])
			data[key].append([begin, end])
		return (data, itern)

	def calculate_node_similarities_parallel(self, key, nodes, itern):
		nodes.sort(key=itemgetter(0)) ## Sort by starting offset
		new_nodes = []
		mapping = {}
		used = set()
		for i in range(0, len(nodes)):
			if i in used:
				continue
			curr_node = nodes[i]
			new_node_nodes = [curr_node] ## add all nodes here that are to be considered as one, then calc centroid
			for j in range(i+1, len(nodes)):
				if j in used:
					continue
				comp_node = nodes[j]
				sim = self.similarity(curr_node, comp_node)
				if sim == 1:
					new_node_nodes.append(comp_node)
					used.add(j)
				elif sim == -1:
					break
				else:
					continue

			new_node = self.stringify(key, self.calculate_new_node(new_node_nodes)) ## already stringified
			for node in new_node_nodes:
				mapping[self.stringify(key, node)] = new_node

		return (mapping, itern)



	def similarity(self, n1, n2):
		lengths = n1[1] - n1[0], n2[1] - n2[0]

		extra = min(lengths) * (1-self.node_similarity)
		if n2[0]-extra > n1[0]:
			return -1
		else:
			overlap = n1[1] - n2[0]
			if n2[1] < n1[1]:
				overlap - n1[1]-n2[1]
			if overlap/max(lengths) > self.node_similarity:
				return 1
			else:
				return 0

	def calculate_new_node(self, new_nodes):
		starts = []
		ends = []
		for node in new_nodes:
			starts.append(node[0])
			ends.append(node[1])
		return [int(sum(starts) / len(starts)), int(sum(ends) / len(ends))]


class Clusterizer:

	def __init__(self, output_folder, min_length, max_length, threads, node_similarity, pre_split, clusters_per_file, min_alignment_score, logger, compress=False):
		self.output_folder = output_folder
		self.min_length = min_length
		self.max_length = max_length
		self.threads = threads
		self.pre_split = pre_split
		self.parallelizer = ParallelJobRunner(output_folder, min_length, max_length, node_similarity, None, compress)
		self.community = CommunityDetector()
		self.clusters_per_file = clusters_per_file
		self.min_alignment_score = min_alignment_score
		self.logger = logger

	def clusterize(self):
		self.logger.info("Starting clusterizing, using {} cores...".format(self.threads))
		data = self.read_data()
		data = self.flatten_data(data)
		nodes = self.find_nodes(data)
		data = self.stringify_data(data)
		mapping = self.calculate_node_similarities(nodes)
		data_list = self.make_data_list(data, mapping)
		self.extract_clusters(data_list, 0)

		## Read the data in parallel, combine results into one dictionary, data = dictionary, key = id (file1), value = list of hsps
	def read_data(self):
		self.logger.info("Reading data...")
		files = os.listdir(self.output_folder + "/batches")
		datas = Parallel(n_jobs=self.threads)(delayed(self.parallelizer.read_data_parallel)(filename, file_index, self.min_alignment_score) for file_index, filename in enumerate(files))
		data = {key: value for data_dictionary in datas for key, value in data_dictionary.items()}
		return data

		## Flatten the data in case keys were pre split before for BLASTing, i.e, file1 split into file1__0_1000, file1__1000_2000...
	def flatten_data(self, data):
		if not self.pre_split:
			return data
		else:
			self.logger.info("Flattening data...")
			## First gather all subkey datas
			temp_data = self.gather_sub_key_data(data)
			## Parallelize flattening
			flattened_datas = Parallel(n_jobs=self.threads)(delayed(self.parallelizer.flatten_data_parallel)(key, value) for key, value in temp_data.items())
			data = {key: value for data_dictionary in flattened_datas for key, value in data_dictionary.items()}
			return data

	def gather_sub_key_data(self, data):
		keys = {}
		temp_data = {}
		for key in data:
			q_key = key.split("__")[0]
			keys.setdefault(q_key, [])
			keys[q_key].append(key)

		for key, value in keys.items():
			temp_data[key] = []
			for sub_key in value:
				temp_data[key].append([sub_key, data[sub_key]])
		return temp_data

		## Finds all nodes (offset_start, offset_end) for every key
	def find_nodes(self, data):
		self.logger.info("Finding nodes...")

		node_dicts = Parallel(n_jobs=self.threads)(delayed(self.parallelizer.find_nodes_parallel)(key, value) for key, value in data.items())
		nodes = {}
		for node_dict in node_dicts:
			for key, value in node_dict.items():
				if key in nodes:
					nodes[key] += value
				else:
					nodes[key] = value

		return nodes


		## Make strings from the hsps values
	def stringify_data(self, data):
		self.logger.info("Stringifying data...")
		data_to_feed = []
		for key in sorted(list(data.keys())):
			data_to_feed.append((key, data[key]))
		stringified_dicts = Parallel(n_jobs=self.threads)(delayed(self.parallelizer.stringify_data_parallel)(key, value, itern) for itern, (key, value) in enumerate(data_to_feed))
		stringified_dicts.sort(key=itemgetter(1))
		data = {}
		for (data_dictionary, itern) in stringified_dicts:
			for key, value in data_dictionary.items():
				data[key] = value
		return data


		## Calculate mean / centroid nodes, so two nodes that are almost same will be considered one
	def calculate_node_similarities(self, nodes):
		self.logger.info("Calculating node similarities...")
		data_to_feed = []
		for key in sorted(list(nodes.keys())):
			data_to_feed.append((key, nodes[key]))
		maps = Parallel(n_jobs=self.threads)(delayed(self.parallelizer.calculate_node_similarities_parallel)(key, value, itern) for itern, (key, value) in enumerate(data_to_feed))
		maps.sort(key=itemgetter(1))
		from collections import OrderedDict
		mapping = OrderedDict()
		for (data_map, itern) in maps:
			mapping.update(data_map)
		return mapping


	def make_data_list(self, data, mapping):
		self.logger.info("Making disjoint data list...")
		data_list = []
		for key in sorted(list(data.keys())):
			pairs = data[key]
			for edgepair in pairs:
				data_list.append((mapping[edgepair[0]], mapping[edgepair[1]]))
		return data_list


	## According to https://stackoverflow.com/a/20167281

	def indices_dict(self, data):
		d = defaultdict(list)
		for i,(a,b) in enumerate(data):
			d[a].append(i)
			d[b].append(i)
		return d

	def disjoint_data_indices(self, data):
		d = self.indices_dict(data)
		d_keys = sorted(list(d.keys()))
		d_set_keys = set(d_keys)
		sets = []
		while len(d):
			while True:
				d_key = d_keys.pop(0)
				if d_key in d:
					break
			que = set(d.pop(d_key))
			ind = set()
			while len(que):
				ind |= que
				que = set([y for i in que for x in data[i] for y in d.pop(x, [])]) - ind
			sets += [ind]
		return sets

	def generate_disjoint_components(self, data):
		return [set([x for i in s for x in data[i]]) for s in self.disjoint_data_indices(data)]

	def extract_clusters(self, data_list, iteration):
		cluster_index = 0
		save_index = 0
		clusters = {}
		for disjoint_set in self.generate_disjoint_components(data_list):
			new_clusters = self.community.detect(nodes, None)
			for new_cluster in new_clusters:
				clusters["cluster_{}".format(cluster_index)] = new_cluster
				cluster_index += 1
			if len(clusters) >= self.clusters_per_file:
				self.save_clusters(clusters, save_index, iteration)
				save_index += 1
				clusters.clear()
		self.save_clusters(clusters, save_index, iteration)

	def save_clusters(self, clusters, save_index, iteration):
		if not os.path.exists("{}/clusters/unfilled/iteration_{}".format(self.output_folder, iteration)):
			os.makedirs("{}/clusters/unfilled/iteration_{}".format(self.output_folder, iteration))
		with gzip.open("{}/clusters/unfilled/iteration_{}/clusters_{}.gz".format(self.output_folder, iteration, save_index), "wt") as gzf:
			gzf.write(json.dumps(clusters))

class ClusterizerVol2(Clusterizer):

	def __init__(self, output_folder, min_length, max_length, threads, node_similarity, pre_split, files_per_iteration, clusters_per_file, min_alignment_score, start_round, end_round, alignment_ranges, logger, compress=False):
		self.output_folder = output_folder
		self.min_length = min_length
		self.max_length = max_length
		self.threads = threads
		self.pre_split = pre_split
		self.node_similarity = node_similarity
		self.parallelizer = ParallelJobRunner(output_folder, min_length, max_length, node_similarity, alignment_ranges, compress)
		self.community = CommunityDetector()
		self.clusters_per_file = clusters_per_file
		self.files_per_iteration = int(files_per_iteration)
		self.minimum_alignment_score = min_alignment_score
		self.alignment_ranges = alignment_ranges
		self.start_round = start_round
		self.end_round = end_round
		self.logger = logger

	def clusterize(self):
		self.logger.info("Starting clusterizing, using {} cores...".format(self.threads))
		current_iteration = 0
		current_round = 0
		if self.start_round > -1:
			current_round = self.start_round
		else:
			current_round = 0
		while True:
			file_count = int(self.get_file_counts(current_round))
			if current_round == 0:
				self.clusterize_current_files(current_round, file_count)
			else:
				self.clusterize_current_files(current_round, file_count)
			current_round += 1
			if not self.must_continue_batches(current_round):
				break

	def clusterize_current_files(self, current_round, file_count):
		current_iteration = 0
		for i in range(0, file_count, self.files_per_iteration):
			self.logger.info("Clusterized {}/{} folders, iteration {}, {} per iteration...".format(i, file_count, current_iteration, self.files_per_iteration))
			data = self.read_data(current_iteration, current_round)
			if current_round == 0:
				data = self.flatten_data(data)
				data = self.stringify_data(data)
			nodes = self.find_nodes(data)
			mapping = self.calculate_node_similarities(nodes)
			data_list = self.make_data_list(data, mapping)
			self.extract_clusters(data_list, current_iteration, current_round)
			data, nodes, mapping, data_list = [], [], [], [] ## CLEAR RAM
			current_iteration += 1

	def find_nodes(self, data):
		self.logger.info("Finding nodes...")
		data_to_feed = []
		for key in sorted(list(data.keys())):
			data_to_feed.append((key, data[key]))
		node_dicts = Parallel(n_jobs=self.threads)(delayed(self.parallelizer.find_nodes_parallel)(key, value, itern) for itern, (key, value) in enumerate(data_to_feed))
		node_dicts.sort(key=itemgetter(1))
		nodes = {}
		for (node_dict, itern) in node_dicts:
			for key, value in node_dict.items():
				if key in nodes:
					nodes[key] += value
				else:
					nodes[key] = value
		return nodes

	def must_continue_batches(self, current_round):
		if current_round == self.end_round:
			return False
		if current_round == 0:
			folders = os.listdir(self.output_folder + "/batches")
		else:
			folders = os.listdir(self.output_folder + "/clusters/unfilled")
			folders = [f for f in folders if "round_{}_".format(current_round-1) in f]
		if len(folders) == 1:
			return False
		else:
			return True

	def get_file_counts(self, current_round):
		if current_round == 0:
			files = os.listdir(self.output_folder + "/batches")
		else:
			files = [f for f in os.listdir(self.output_folder + "/clusters/unfilled/") if "round_{}".format(current_round-1) in f]
		return len(files)

	def read_data(self, current_iteration, current_round):
		self.logger.info("Reading data...")
		files = []
		if current_round == 0:
			folder = self.output_folder + "/batches"
			files = natsorted(os.listdir(folder))
			files = files[current_iteration*self.files_per_iteration:(current_iteration+1)*self.files_per_iteration]

		else:
			folders = natsorted([f for f in os.listdir(self.output_folder + "/clusters/unfilled/") if "round_{}".format(current_round-1) in f])
			folders = folders[current_iteration*self.files_per_iteration:(current_iteration+1)*self.files_per_iteration]
			for folder in folders:
				folder_files = natsorted(os.listdir(self.output_folder + "/clusters/unfilled/" + folder))
				for folder_file in folder_files: files.append(self.output_folder + "/clusters/unfilled/" + folder + "/" + folder_file)

		if current_round == 0:
			datas = Parallel(n_jobs=self.threads)(delayed(self.parallelizer.read_data_parallel_iterations)(filename, file_index, self.minimum_alignment_score, "batches") for file_index, filename in enumerate(files))
			data = {key: value for (data_dictionary, itern) in datas for key, value in data_dictionary.items()}
		else:
			datas = Parallel(n_jobs=self.threads)(delayed(self.parallelizer.read_data_parallel_iterations)(filename, file_index, self.minimum_alignment_score, "clustered") for file_index, filename in enumerate(files))
			datas.sort(key=itemgetter(1))
			data = {}
			for (data_dict, itern) in datas:
				for start_doc_key, nodes in data_dict.items():
					data[start_doc_key] = data.get(start_doc_key, [])
					data[start_doc_key] += nodes
		return data

	def extract_clusters(self, data_list, iteration, current_round):
		cluster_index = 0
		save_index = 0
		clusters = {}
		for disjoint_index, disjoint_set in enumerate(self.generate_disjoint_components(data_list)):
			nodes = list(disjoint_set)
			edges = None
			new_clusters = self.community.detect(nodes, edges)
			for new_cluster in new_clusters:
				clusters["cluster_{}".format(cluster_index)] = new_cluster
				cluster_index += 1
			if len(clusters) >= self.clusters_per_file:
				self.save_clusters(clusters, save_index, iteration, current_round)
				save_index += 1
				clusters.clear()
		self.save_clusters(clusters, save_index, iteration, current_round)

	def save_clusters(self, clusters, save_index, iteration, current_round):
		if not os.path.exists("{}/clusters/unfilled/round_{}_iteration_{}".format(self.output_folder, current_round, iteration)):
			os.makedirs("{}/clusters/unfilled/round_{}_iteration_{}".format(self.output_folder, current_round, iteration))
		with gzip.open("{}/clusters/unfilled/round_{}_iteration_{}/clusters_{}.gz".format(self.output_folder, current_round, iteration, save_index), "wt") as gzf:
			gzf.write(json.dumps(clusters))

if __name__ == "__main__":
	parser = argparse.ArgumentParser(description="Clusterizing the results.")
	parser.add_argument("--output_folder", help="Output folder. This is the folder done by data_preparer", required=True)
	parser.add_argument("--min_length", help="Minimum length of found hit. Default = 0", default=0, type=int)
	parser.add_argument("--max_length", help="Maximum length of found hit. Default = 100000", default=100000, type=int)
	parser.add_argument("--node_similarity", help="Minimum node similarity to be considered the same. Default = 0.90", type=float, default=0.90)
	parser.add_argument("--threads", help="Number of threads to use. Default = 1", default=1, type=int)
	parser.add_argument("--pre_split", help="If the data is presplit and needs to be flattened. Default = False", action="store_true", default=False)
	parser.add_argument("--compress", help="If the data should be compressed mid clusterizing. Default = False", default=False)
	parser.add_argument("--files_per_iter", help="Number of files to read for iteration. Default 20", default=20)
	parser.add_argument("--clusters_per_file", help="Number of clusters to save per file. Default = 1000", default=1000, type=int)
	parser.add_argument("--min_align_score", help="Minimum alignment score. i.e how similar two hits are. Default = 0.0, so BLAST decides everything", default=0.0, type=float)
	parser.add_argument("--alignment_ranges", help="Hit length ranges and what min align score to use there. e.g. 0,0.85,100;100,0.75,150", default=None, type=str)
	parser.add_argument("--start_round", help="Dev option.", default=-1, type=int)
	parser.add_argument("--end_round", help="Dev option.", default=-1, type=int)
	parser.add_argument("--log_file", help="Whether to log output to a log file as well as stdin", default=None)
	args = parser.parse_args()

	logger = get_logger(args.log_file)

	c = ClusterizerVol2(output_folder=args.output_folder, min_length=args.min_length, max_length=args.max_length, threads=args.threads, node_similarity=args.node_similarity,  pre_split=args.pre_split, compress=args.compress, files_per_iteration=args.files_per_iter, clusters_per_file=args.clusters_per_file, min_alignment_score=args.min_align_score, start_round=args.start_round, end_round=args.end_round, alignment_ranges=args.alignment_ranges, logger=logger)
	c.clusterize()
	logger.info("Done clusterizing...")
