import numpy as np
import time

from .dataset import AbstractDataSet, DataSetColumn, DataSetResult
from ..j_utils.parallelism import parallel_exec
from ..j_utils.j_log import log
from ..j_utils.function import match_params, not_optional_args
from ..j_utils.collections import if_else


########################################################################################################################
class DataSetMap(AbstractDataSet):
    def __init__(self, dataset, mapping, keep_all_columns=True, name='mapping'):
        """
        :type dataset: AbstractDataSets
        """
        super(DataSetMap, self).__init__(name, dataset, pk_type=dataset.pk.dtype)

        mapped_cols = set()
        for column, real_columns in mapping.items():
            if isinstance(real_columns, str):
                mapping[column] = [real_columns]
                mapped_cols.add(real_columns)
            elif isinstance(real_columns, (list, tuple)):
                mapped_cols.update(real_columns)
            else:
                raise ValueError('Invalid mapping value: %s.\n '
                                 'Valid value are column name for mapping and list or tuple for concatenation.'
                                 % real_columns)

        if keep_all_columns:
            for c in set(dataset.columns_name())-mapped_cols:
                mapping[c] = [c]

        self.concatenation_map = mapping
        for column, real_columns in mapping.items():
            c = dataset.column_by_name(real_columns[0])
            shape = c.shape
            for real_column in real_columns[1:]:
                s = dataset.column_by_name(real_column).shape
                assert s[1:] == shape[1:]
                shape = tuple([shape[0]+s[0]] + list(s[1:]))

            self.add_column(column, shape, c.dtype)

    def _generator(self, gen_context):
        columns = gen_context.columns

        gen_columns = set()
        columns_map = {}
        for c_name in columns:
            c_parents = self.concatenation_map[c_name]
            gen_columns.update(set(c_parents))
            i = 0
            for c_parent in c_parents:
                column_parent = self.parent_dataset.column_by_name(c_parent)
                n = column_parent.shape[0] if column_parent.shape else 0
                columns_map[c_parent] = (c_name, i, n)
                i += n
            columns_map['pk'] = ('pk', 0, 0)
        gen_columns = list(gen_columns)
        gen = gen_context.generator(self._parent, columns=gen_columns)

        while not gen_context.ended():
            _, N, weakref = gen_context.create_result()
            r = weakref()
            gen.next(copy={c_parent: r[c_name][:, i:i+n] if n > 0 else r[:, c_name]
                           for c_parent, (c_name, i, n) in columns_map.items()}, limit=N, r=r)
            r = None
            yield weakref

    @property
    def size(self):
        return self._parent.size


########################################################################################################################
class DataSetShuffle(AbstractDataSet):
    def __init__(self, dataset, indices=None, subgen=1, name='shuffle', rnd=None):
        """
        :type dataset: AbstractDataSets        if args
        """
        super(DataSetShuffle, self).__init__(name, dataset, pk_type=dataset.pk.dtype)
        self._columns = dataset.copy_columns(self)

        self.rnd = rnd

        self.indices = None if indices is None else np.asarray(indices, dtype=np.uint32)
        self.random_indices = indices is None

        self.subgen = max(subgen,0) if isinstance(subgen, int) else 0
        self.subgen_range = None
        self.subgen_index = None

        if subgen and subgen != 1 and not self.random_indices:
            if isinstance(subgen, list):
                self.subgen = len(subgen)
                self.subgen_range = subgen
            else:
                log.warn("With not random indicies, subgenerators range must be specified. "
                         "Falling back to no subgenerators...")
                self.subgen = 0

    def _generate_random_sequence(self):
        if self.subgen > 1:
            # Compute (start, end) for each subgen
            subgen_range = [(int(round(_ * self.size / self.subgen)), int(round((_ + 1) * self.size / self.subgen)))
                        for _ in range(self.subgen)]

            # Create one-hot
            rand_seq = np.zeros((self.size, self.subgen), dtype=np.uint32)
            for i, (start, end) in enumerate(subgen_range):
                n = end-start
                rand_seq[start:start + n, :] = [1 if i == _ else 0 for _ in range(self.subgen)]
            # Shuffle one-hot
            self.rnd.shuffle(rand_seq)

            # Compute the table of subgenerator own indexes by cumsum one-hot
            rand_seq_id = rand_seq*rand_seq.cumsum(axis=0)

            # Replace every one in the one-hot table by the starting index of its sub-generator
            rand_seq_start = np.multiply(rand_seq, [start for (start, end) in subgen_range])

            # Compute final random sequence by adding the sub-generators own indexes and starting index
            indices = np.asarray((rand_seq_start+rand_seq_id).sum(axis=1), dtype=np.uint32)-1

            # Compute subgenerator indexes
            np.multiply(rand_seq, np.arange(len(subgen_range), dtype=np.uint32), out=rand_seq)
            rand_seq = rand_seq.sum(axis=1)

            return indices, subgen_range, rand_seq
        else:
            rand_seq = np.arange(self.parent_dataset.size, dtype=int)
            self.rnd.shuffle(rand_seq)
            return rand_seq

    def _setup_determinist(self):
        if self.random_indices:
            seq = self._generate_random_sequence()
            if isinstance(seq, tuple):
                self.indices, self.subgen_range, self.subgen_index = seq
            else:
                self.indices = seq
        else:
            if self.subgen > 1:
                if self.subgen_index is None:
                    indices_map = np.zeros(dtype=np.int32, shape=(self.parent_dataset.size,))-1
                    for i, s in enumerate(self.subgen_range):
                        indices_map[s[0]:s[1]] = i
                    self.subgen_index = indices_map[self.indices]

    def _generator(self, gen_context):
        columns = gen_context.columns

        if gen_context.determinist or not self.random_indices:
            indices = self.indices
            subgen_range = self.subgen_range
        else:
            if self.subgen:
                indices, subgen_range = self._generate_random_sequence()
            else:
                indices = self._generate_random_sequence()
                subgen_range = None

        #indices = indices[gen_context.start_id:gen_context.end_id]

        # Setup subgenerators
        subgen = []
        subgen_index = None
        async_subgen = None
        if self.subgen > 1:
            valid_indices = indices[gen_context.start_id:gen_context.end_id+1]
            start = valid_indices.min()
            end = valid_indices.max()+1

            valid_subgen = []
            valid_ranges = []
            for i, (gen_start, gen_end) in enumerate(subgen_range):
                if gen_start < end and end > start:
                    gen_start = max(gen_start, start)
                    gen_end = min(gen_end, end)
                    valid_ranges.append((gen_start, gen_end))
                    valid_subgen.append(i)
            subgen_range = valid_ranges
            valid_subgen = np.array(valid_subgen)
            # valid_subgen: [-1 if _ not in valid_subgen else valid_subgen.index(_) for _ in range(self.subgen)]
            valid_subgen = np.array([if_else(np.where(valid_subgen == _)[0], lambda x: len(x) > 0, [-1])[0]
                                     for _ in range(self.subgen)])

            if not 1 < gen_context.ncore <= len(subgen_range):
                async_subgen = gen_context.ncore > len(subgen_range)
                subgen_index = self.subgen_index
                # Setup subgenerator
                for i, (gen_start, gen_end) in enumerate(subgen_range):
                    is_last = i == len(subgen_range)-1
                    gen = gen_context.generator(self._parent, start=gen_start, end=gen_end, n=1,
                                                parallel=async_subgen and not is_last,
                                                ncore=round(i*gen_context.ncore/len(subgen_range)))
                    if async_subgen:
                        gen.setup()     # Begin computation if parallel
                    subgen.append(gen)

            else:
                async_subgen = True

                # Split subgen
                s_split = []
                indices_map = np.zeros(dtype=np.int32, shape=(self.parent_dataset.size,))-1
                for i in range(gen_context.ncore):  # For each core select the subset of valid subgen
                    i0 = round(i*len(subgen_range)/gen_context.ncore)
                    i1 = round((i+1)*len(subgen_range)/gen_context.ncore)
                    for start, end in valid_ranges[i0:i1]:
                        indices_map[start:end] = i
                    s_split.append((i0, i1))
                subgen_index = indices_map[valid_indices]
                del indices_map
                for i, (i0, i1) in enumerate(s_split):  # For each core setup dataset and generator
                    is_last = i == len(s_split) - 1
                    s_indices = indices[subgen_index==i]
                    s_dataset = DataSetShuffle(self._parent, indices=s_indices, subgen=valid_ranges[i0:i1])
                    s_dataset.subgen_index = valid_subgen[self.subgen_index[subgen_index==i]]-i0
                    gen = gen_context.generator(s_dataset, n=1, ncore=1, parallel='thread' if is_last else 'process')
                    gen.setup()
                    subgen.append(gen)
        else:
            if gen_context.ncore > 1:
                for i in range(gen_context.ncore):
                    gen = gen_context.generator(self._parent, n=1, start=indices[gen_context.start_id],
                                                ncore=1, parallel=True)
                    gen.seq_id = None
                    subgen.append(gen)
            else:
                subgen.append(gen_context.generator(self._parent, n=1, start=0, end=self._parent.size, ncore=1))

        while not gen_context.ended():
            i_global, n, weakref = gen_context.create_result()
            r = weakref()

            if self.subgen <= 1:
                seq = list(indices[i_global:i_global+n])
                if len(subgen) > 1:  # -- Mutliple core, No subgen --
                    waiting_seq = len(seq)
                    while waiting_seq:
                        waiting = True
                        for s in subgen:
                            if s.seq_id is not None and s.poll(copy=r[s.seq_id:s.seq_id+1], r=r, ask_next=False):
                                waiting = False
                                s.seq_id = None
                                waiting_seq -= 1

                            if seq and s.seq_id is None:     # Ask next
                                s.seq_id = n - len(seq)
                                s.ask(seq.pop(0))

                        if waiting:
                            time.sleep(1e-3)

                else:       # -- Single core, No subgen --
                    for i, seq_id in enumerate(seq):
                        subgen[0].next(copy=r[i:i+1], r=r, seek=seq_id)
                    if gen_context.is_last() or indices[i_global+n] != indices[i_global+n-1]+1:
                        subgen[0].clean()
            else:
                seq_subgens = subgen_index[i_global:i_global+n]
                if async_subgen:    # -- Async subgen --
                    for i, sub_id in enumerate(seq_subgens):
                        subgen[sub_id].next(copy=r[i:i+1], r=r)
                else:               # -- Sync subgen --
                    seq_indexes = self.indices[i_global:i_global + n]
                    for i, (sub_id, seq_id) in enumerate(zip(seq_subgens, seq_indexes)):
                        try:
                            subgen[sub_id].next(copy=r[i:i+1], r=r, seek=seq_id)
                        except StopIteration as e:
                            import traceback
                            traceback.print_last()
                            raise e
            r = None
            yield weakref

        for s in subgen:
            s.clean()

    @property
    def size(self):
        return self._parent.size if self.indices is None else len(self.indices)


########################################################################################################################
class DataSetJoin(AbstractDataSet):
    def __init__(self, datasets, verbose=False, **kwargs):
        """
        :param datasets: A list of dataset to join. Each element of the list should be either:
                          - a dataset (the primary key column is used for the join)
                          - a tuple containing a dataset and the name of the column where the join should be performed
                          - a dataset column: the dataset will be join on this column

        :param kwargs: For each elements of kwargs, a column will be created in this dataset.
                       kwargs elements should be either  tuple containing the index of the dataset and the name of
                       its column on which the join should be processed, or simply this column.
                       If kwargs is empty, all columns of the datasets are inserted in this dataset
                       as DATASETNAME_COLUMNNAME.

                       A 'pk' parameters can be passed to specify which column should be used as primary key
                       (by default the primary key of the first dataset is used)

        WARNING: FOR THE TIME BEING, THIS DATASET WILL PERFOM A NAIVE JOIN WHEN THE DATASET IS CREATED.
                 THIS PROCESS CAN BE LONG AND MEMORY CONSUMING, DON'T USE IT ON LARGE DATABASES!!!!!!
                 (all the columns on which the join is performed are read and ordered,
                 indexes are stored permanently in RAM...)

        NOTE:    All dataset will be accessed in ascending order of their join column
        """

        #  ---  REFORMATING PARAMETERS  ---
        dataset_tuples = []
        datasets_list = []
        for dataset in datasets:
            if isinstance(dataset, DataSetColumn):
                join_column = dataset.name
                dataset = dataset.dataset
            elif isinstance(dataset, tuple):
                join_column = dataset[1]
                dataset = dataset[0]
            elif isinstance(dataset, AbstractDataSet):
                join_column = 'pk'
            else:
                raise NotImplementedError
            dataset_tuples.append((dataset, join_column))
            datasets_list.append(dataset)
        datasets = dataset_tuples

        if 'pk' in kwargs:
            pk = getattr(datasets_list[kwargs['pk'][0]], kwargs['pk'][1])
            del kwargs['pk']
        else:
            pk = datasets_list[0].pk

        for name, column in kwargs.items():
            if isinstance(column, DataSetColumn):
                kwargs[name] = (datasets_list.index(column.dataset), column.name)

        #  ---  READING AND SIMPLIFYING DATASETS  ---
        if len(kwargs):
            used_datasets = {_[0] for n, _ in kwargs.items()}
        else:
            used_datasets = {_ for _ in range(len(datasets))}

        datasets_name = []
        simp_dataset_map = [-1]*len(datasets)
        simplified_datasets = []
        join_columns = []
        for dataset_id in used_datasets:
            dataset, join_column = datasets[dataset_id]
            root_name = dataset.dataset_name
            name = root_name
            i = 0
            while name in datasets_name:
                i += 1
                name = root_name + '(%i)' % i
            datasets_name.append(name)
            simp_dataset_map[dataset_id] = len(simplified_datasets)
            simplified_datasets.append(dataset)
            join_columns.append((dataset, join_column))
        datasets = simplified_datasets

        super(DataSetJoin, self).__init__('Join', datasets, pk_type=pk.dtype)

        #  ---  DEFINING COLUMNS ---
        self._pk_foreign_col = (datasets.index(pk.dataset), pk.name)
        self._columns_map = {}
        if len(kwargs):     # Only specific columns of joined datasets must be used
            for column_name, column in kwargs.items():
                dataset_id, foreign_name = column
                dataset_id = simp_dataset_map[dataset_id]
                if not isinstance(column, tuple) or len(column) != 2:
                    continue
                remote_c = datasets[dataset_id].column_by_name(foreign_name)
                self.add_column(column_name, remote_c.shape, remote_c.dtype)
                self._columns_map[column_name] = (dataset_id, foreign_name)
        else:               # All columns of joined datasets will be used
            for dataset_id, (dataset, dataset_name) in enumerate(zip(datasets, datasets_name)):
                for column in dataset.columns:
                    dataset_id = simp_dataset_map[dataset_id]
                    column_name = '%s_%s' % (dataset_name, column.name)
                    self.add_column(column_name, column.shape, column.dtype)
                    self._columns_map[column_name] = (dataset_id, column.name)

        #  ---  JOIN DATASETS  ---
        # Read and ordering join columns
        join_datas = []
        ordered_join = []
        min_size = datasets[0].size
        for dataset, column in join_columns:
            min_size = min(min_size, dataset.size)

            gen = dataset.generator(n=1, columns=[column])
            data = next(gen)[column]
            join_data = np.zeros((dataset.size,),  dtype=data.dtype)
            join_data[0] = data[0]
            for i in range(1, dataset.size):
                join_data[i] = next(gen)[column][0]

            join_datas.append(join_data)
            ordered_join.append(np.argsort(join_data))

        self.ordered_join = ordered_join

        # Iterating through join columns to find matches
        join = np.zeros((min_size, len(join_datas)), dtype=int)
        n = 0
        all_it = np.zeros((len(join_columns)-1,), dtype=int)
        for it1 in range(join_columns[0][0].size):
            ref = join_datas[0][ordered_join[0][it1]]
            for id_it in range(len(all_it)):
                join_data = join_datas[id_it+1]
                ds_ordered_join = ordered_join[id_it+1]
                skipped = False
                while all_it[id_it]+1 < len(ds_ordered_join) and (join_data[ds_ordered_join[all_it[id_it]+1]] <= ref
                      or join_data[ds_ordered_join[all_it[id_it]+1]] == join_data[ds_ordered_join[all_it[id_it]]]):
                    if skipped and verbose:
                        log.info("%s (dataset: %s)  will be skipped"
                              % (join_data[ds_ordered_join[all_it[id_it]]], datasets[id_it+1].dataset_name))
                    all_it[id_it] += 1
                    skipped = True

            is_matched = True
            for id_it in range(len(all_it)):
                if ref != join_datas[id_it+1][ordered_join[id_it+1][all_it[id_it]]]:
                    is_matched = False
                    break

            if is_matched:
                join[n, 0] = ordered_join[0][it1]
                join[n, 1:] = [order[it] for order, it in zip(ordered_join[1:], all_it)]
                n += 1
            elif verbose:
                log.info("%s (dataset: %s)  will be skipped"
                      % (ref, datasets[0].dataset_name))

            end = False
            for id_it in range(len(all_it)):
                if all_it[id_it]+1 == len(ordered_join[id_it+1]):
                    end = True
                    break
            if end:
                break

        # Storing joined index for each dataset
        self._join = join[:n, :]

    @property
    def size(self):
        return self._join.shape[0]

    def _generator(self, gen_context):
        columns = gen_context.columns

        # Initialise to match asked columns
        datasets_columns = [[] for _ in self.parent_datasets]
        reverse_columns_map = [{} for _ in self.parent_datasets]
        for c_name, (dataset_id, foreign_name) in self._columns_map.items():
            if c_name in columns:
                datasets_columns[dataset_id].append(foreign_name)
                reverse_columns_map[dataset_id][foreign_name] = c_name

        datasets_columns[self._pk_foreign_col[0]].append(self._pk_foreign_col[1])
        reverse_columns_map[self._pk_foreign_col[0]][self._pk_foreign_col[1]] = 'pk'
        ngen = len(datasets_columns)
        generators = [None, -1] * ngen

        intime_gens = [False] + [i < gen_context.ncore for i in reversed(range(1, ngen))]
        if gen_context.ncore <= ngen:
            ncore_gens = [1]*ngen
        else:
            free_core = gen_context.ncore-ngen
            mean_ncore = free_core//ngen
            ncore_gens = [mean_ncore + (1 if i < free_core % ngen else 0) for i in range(ngen)]

        while not gen_context.ended():
            global_i, n, weakref = gen_context.create_result()
            r = weakref()

            for i in range(n):
                # Setup generators
                for dataset_id, gen in enumerate(generators):
                    needed_index = self._join[global_i + i, dataset_id]
                    if gen[1] != needed_index or gen[0] is None:
                        dataset = self.parent_datasets[dataset_id]
                        generators[dataset_id][0] = gen_context.generator(dataset, start=needed_index, end=dataset.size,
                                                                          n=1, columns=datasets_columns[dataset_id],
                                                                          intime=intime_gens[dataset_id],
                                                                          ncore=ncore_gens[dataset_id])
                        generators[dataset_id][1] = needed_index

                # Reading generators
                for gen_id, gen in enumerate(generators):
                    gen[0].next(copy={c: r[i:i+1, reverse_columns_map[gen_id][c]] for c in gen[0].columns
                                      if c in reverse_columns_map[gen_id]}, r=r)

                # Updating generator index
                for gen in generators:
                    gen[1] += 1
            r = None
            yield weakref

    def subset(self, start=0, end=None, *args):
        from copy import deepcopy
        if len(args) == 1:
            start = 0
            end = args[0]
        elif len(args) == 2:
            start = args[0]
            end = args[1]
        if not 0 <= start < self.size:
            start = 0
        if not start <= end < self.size:
            end = self.size

        sub = deepcopy(self)
        sub._name += '_Subset'
        sub._join = self._join[start:end, :]
        return sub


########################################################################################################################
class DataSetConcatenate(AbstractDataSet):
    def __init__(self, datasets, columns=None, name='DataSetConcatenate'):
        """
        Concatenate multiple datasets into one.
        (The resulting dataset length equals the sum of the provided datasets length)
        :param datasets: A list of dataset to concatenate.
        :param columns: columns determines which columns should be included in this dataset.
                    - None: Only columns included in every concatenated datasets are included
                    - List of column name: Every name of this list must refer to a column of at least one dataset.
                                           If a columns is not present in a dataset its data will be filled with 0.
                    - Dictionary: keys must be a column name included in at least one dataset,
                                  values are the default value used when the column is not included in a dataset.
        """
        if not len(datasets):
            raise ValueError('datasets list to concatenate is empty!')

        # -- Check columns --
        if columns is None:
            columns = [_ for _ in datasets[0].columns_name()]
            for d in datasets[1:]:
                columns = [_ for _ in d.columns_name() if _ in columns]
            if not columns:
                raise ValueError('Provided datasets for concatenations have no columns in common')
        if isinstance(columns, (list, tuple)):
            columns = {_: 0 for _ in columns}
        elif isinstance(columns, str):
            columns = {columns: 0}
        elif not isinstance(columns, dict):
            raise ValueError('Columns must either be of type None, str, list, tuple or dict (not: %s)' % type(columns))

        # Check presence and compatiblity
        columns_default = columns
        columns = {}
        for col_name, col_default in columns_default.items():
            col = None
            for d in datasets:
                if col_name in d.columns_name():
                    d_column = d.column_by_name(col_name)
                    if col is None:
                        col = d_column
                        # Check default value compatibility
                        if col.dtype == str and col_default == 0:
                            col_default = ''
                            columns_default[col_name] = col_default
                        if not isinstance(col_default, np.ndarray):
                            columns_default[col_name] = np.full(shape=col.shape, fill_value=col_default, dtype=col.dtype)
                        else:
                            if col_default.shape != col.shape:
                                raise ValueError('Default value shape must match columns shape.\n'
                                                 'Column %s expected shape: %s, but the given default value shape is: %s'
                                                 % (repr(col_name), col.shape, col_default.shape))
                            if col_default.dtype != col.dtype:
                                raise ValueError('Default value dtype must match columns dtype.\n'
                                                 'Column %s expected dtype: %s, but the given default value dtype is: %s'
                                                 % (repr(col_name), col.dtype, col_default.dtype))
                    else:
                        if col.shape != d_column.shape:
                            raise ValueError('Columns shape must the same across datasets.\n'
                                             'Column %s expected shape: %s, but shape from dataset %s is: %s'
                                             % (repr(col_name), col.shape, d.dataset_name, d_column.shape))
                        if col.dtype != d_column.dtype:
                            raise ValueError('Columns dtype must the same across datasets.\n'
                                             'Column %s expected type: %s, but type from dataset %s is: %s'
                                             % (repr(col_name), col.dtype, d.dataset_name, d_column.dtype))

            if col is None:
                raise ValueError('Column %s is not included in any concatenated datasets.' % col_name)
            columns[col_name] = col.shape, col.dtype

        # -- Setup dataset --
        super(DataSetConcatenate, self).__init__(name=name, parent_datasets=datasets, pk_type=str)
        self._columns = [DataSetColumn(name, shape, dtype, self) for name, (shape, dtype) in columns.items()]
        self._columns_default = columns_default

        self._datasets_start_index = []
        start = 0
        for d in self.parent_datasets:
            self._datasets_start_index.append(start)
            start += d.size

    def _generator(self, gen_context):
        from bisect import bisect_right

        columns = gen_context.columns

        copy_cols = []
        default_cols = []
        gen_end_index = 0
        parent_gen = None

        def read_from_parent(result, global_i, n):
            r = parent_gen.next(copy={c: result[global_i:global_i + n, c] for c in copy_cols}, limit=n, r=result)
            for i in range(n):
                result[global_i+i, 'pk'] = parent_gen.dataset.dataset_name + '|' + str(r[i, 'pk'])
                for c in default_cols:
                    result[global_i+i, c] = self._columns_default[c]

        while not gen_context.ended():
            i_global, N, weakref = gen_context.create_result()
            r = weakref()

            i = 0

            while i < N:
                if parent_gen is None:
                    # Setup next generator
                    dataset_id = bisect_right(self._datasets_start_index, i_global+i)-1
                    dataset = self.parent_datasets[dataset_id]
                    gen_start_index = self._datasets_start_index[dataset_id]
                    gen_end_index = min(gen_start_index + dataset.size, gen_context.end_id)

                    copy_cols = [_ for _ in dataset.columns_name() if _ in columns]
                    default_cols = [_ for _ in columns if _ not in copy_cols]
                    parent_gen = gen_context.generator(dataset, n=N, columns=copy_cols,
                                                       start=i_global + i - gen_start_index,
                                                       end=min(gen_context.end_id-gen_start_index, dataset.size))

                n = min(N - i, gen_end_index - i_global - i)
                try:
                    result = parent_gen.next(copy={c: r[i:i + n, c] for c in copy_cols}, limit=n, r=r)
                except StopIteration:
                    parent_gen = None
                    continue

                if parent_gen.ended():
                    parent_gen = None

                for i_pk in range(n):
                    r[i + i_pk, 'pk'] = parent_gen.dataset.dataset_name + '|' + str(result[i_pk, 'pk'])
                    for c in default_cols:
                        r[i + i_pk, c] = self._columns_default[c]
                i += n

            r = None
            yield weakref

    @property
    def size(self):
        return sum(_.size for _ in self.parent_datasets)


########################################################################################################################
class DataSetApply(AbstractDataSet):
    """
    Apply a function to some columns of a dataset, all other columns are copied
    """
    def __init__(self, dataset, function, columns=None, same_size_type=False, columns_type_shape=None, name='apply'):
        """
        :param dataset: Dataset on which the function should be applied
        :param function: the function to apply to the dataset. The function can be apply element-wise or batch-wise.
                         Parameters of the function can be:
                            - x (for element-wise processing)
                            - batch (for batch-wise processing)
                            - n: batch size
                            - column: the column name from which the x or batch were read
                            - **columnName**: Column element or batch
                            - **columnName_shape**: Column shape
        :param columns: Describe to which columns the function should be applied and which column should be created.
                        Columns can be either:
                            - A column name: The function will be applied to the specified column
                            - A list of column names: The function will be applied to each column individually
                            - A dictionary of new column: The keys of the dictionary are the name of the created columns
                                   (if a tuple is specified as a key, function should return multiple values)
                                   The values are the column sources (if multiples columns are passed, x, batch and
                                   columns are list)
        :param same_size_type: if False, the apply_function will be tried on a sample of the dataset to infer the shape of
                          the columns modified by the apply_function.
        """
        super(DataSetApply, self).__init__(name, dataset, pk_type=dataset.pk.dtype)
        f_params = not_optional_args(function)
        if ('x' in f_params) == ('batch' in f_params):
            raise ValueError('Function to be applied must have either x or batch as parameters.')

        # ---  HANDLE COLUMNS  ---
        parent_columns_name = dataset.columns_name()
        parent_shared_columns = dataset.copy_columns(self)
        own_columns = []
        self._columns_parent = {}
        self._apply_col_dict = {}
        self._apply_columns = []
        n_in = None
        n_out = None
        if columns is None:
            columns = self.columns_name()
        if isinstance(columns, list):
            columns = {_: _ for _ in columns}
        elif isinstance(columns, str):
            columns = {columns: columns}
        for own_c, parent_c in columns.items():
            if isinstance(own_c, str):
                own_c = (own_c,)
            if parent_c is None or not parent_c:
                parent_c = []
            elif isinstance(parent_c, str):
                parent_c = (parent_c,)
            if len(parent_c) != len(own_c):
                same_size_type = False

            # Check parent columns
            for c in parent_c:
                if c not in parent_columns_name:
                    raise ValueError('%s is not a columns of %s!' % (c, dataset.dataset_name))
            if n_in is None:
                n_in = len(parent_c)
            elif n_in != len(parent_c):
                raise ValueError('Columns mapping should have the same number of parent columns (n_in: %i)!' % n_in)
            # Removing parent columns from
            parent_shared_columns = [_ for _ in parent_shared_columns if _.name not in parent_c]

            # Check own columns
            if n_out is None:
                n_out = len(own_c)
            elif n_out != len(own_c):
                raise ValueError('Columns mapping should have the same number of new columns (n_out: %i)!' % n_out)

            for c_id, c in enumerate(own_c):
                for _ in own_columns:
                    if _.name == c:
                        raise ValueError('%s is already a column of %s!' % (c, self.dataset_name))
                # Try to infer column shape and type
                c_shape = None
                c_dtype = None
                if same_size_type:
                    _ = dataset.column_by_name(parent_c[c_id])
                    c_shape = _.shape
                    c_dtype = _.dtype

                # Apply column
                own_columns.append(DataSetColumn(c, c_shape, c_dtype, self))
                self._columns_parent[c] = parent_c
            self._apply_col_dict[own_c] = parent_c
            self._apply_columns += own_c

        self._columns = parent_shared_columns + own_columns
        for c in parent_shared_columns:
            self._columns_parent[c.name] = (c.name,)

        # ---  HANDLE FUNCTION  ---
        self._f = function
        self._elemwise = 'x' in f_params
        self._f_columns = [_ for _ in dataset.columns_name() if _ in f_params]
        self._f_kwargs = {c.name+'_shape': c.shape for c in self._columns}
        self._n = 1

        # ---  INFER COLUMN SHAPE AND TYPE  ---
        if same_size_type:
            columns_type_shape = {c_own: (dataset.column_by_name(c_par).dtype, dataset.column_by_name(c_par).shape)
                                  for c_own, c_par in columns.items()}
        if columns_type_shape is not None and len(columns_type_shape) == len(columns):
            for c_own in self._apply_col_dict.keys():
                for c in c_own:
                    col = self.column_by_name(c)
                    col._dtype = columns_type_shape[c][0]
                    col._shape = columns_type_shape[c][1]
        else:
            sample = dataset.read_one(0, columns=self.f_columns([_.name for _ in own_columns]), extract=False)
            for c_own, c_parent in self._apply_col_dict.items():
                if len(c_parent) == 1:
                    c_parent = c_parent[0]
                kwargs = self._f_kwargs.copy()
                if self._elemwise:
                    kwargs.update({_: sample[0, _] for _ in self._f_columns})
                    c_samples = match_params(self._f, x=sample[0, c_parent], column=c_parent, n=0, **kwargs)
                else:
                    kwargs.update({_: sample[_] for _ in self._f_columns})
                    c_samples = match_params(self._f, batch=sample[c_parent], column=c_parent, n=1, **kwargs)
                if not isinstance(c_samples, tuple):
                    c_samples = [c_samples]
                else:
                    c_samples = list(c_samples)
                if isinstance(c_samples[0], list):
                    if not self._elemwise:
                        raise ValueError('Multiple rows return is not handled with batch-wise function.')
                    self._n = len(c_samples[0])
                    for c_sample_id, c_sample in enumerate(c_samples):
                        if len(c_sample) != self._n:
                            raise ValueError('All returned element should have the same length (n: %i)' % self._n)
                        c_samples[c_sample_id] = c_sample[0]
                elif not self._elemwise:
                    c_samples = [_[0] for _ in c_samples]

                for c_name, c_sample in zip(c_own, c_samples):
                    col = self.column_by_name(c_name)
                    if isinstance(c_sample, np.ndarray):
                        col._shape = c_sample.shape
                        col._dtype = c_sample.dtype
                    else:
                        col._shape = ()
                        col._dtype = type(c_sample) if type(c_sample) != str else 'O'
        self._f_kwargs = {c.name + '_shape': c.shape for c in self._columns}

    def _generator(self, gen_context):
        i_global = gen_context.start_id
        n = gen_context.n
        columns = gen_context.columns

        if self._elemwise:
            parent_gen = gen_context.generator(self._parent, n=1, columns=self.f_columns(columns),
                                               start=gen_context.start_id//self._n, end=gen_context.end_id//self._n)
        else:
            parent_gen = gen_context.generator(self._parent, columns=self.f_columns(columns),
                                               start=gen_context.start_id//self._n, end=gen_context.end_id//self._n)

        kwargs = self._f_kwargs.copy()

        result = None
        f_results = {}
        while not gen_context.ended():
            i_global, n, weakref = gen_context.create_result()
            r = weakref()

            if self._elemwise:
                # Element wise
                for i in range(n):

                    # Retreive data, store f results in f_results
                    if result is None:
                        result = parent_gen.next(copy={c: r[i:i+1, c] for c in r.columns_name() if c not in self._apply_columns},
                                                 limit=1, r=r)
                        kwargs.update({_: result[0, _] for _ in self._f_columns})
                        f_results = {}
                        for c in r.columns_name():
                            if c in self._apply_columns and c not in f_results:
                                for c_own, c_parent in self._apply_col_dict.items():
                                    if c not in c_own:
                                        continue
                                    if len(c_parent) == 1:
                                        c_parent = c_parent[0]
                                    f_result = match_params(self._f, x=result[0, c_parent], columns=c_parent, n=0,
                                                            **kwargs)
                                    if not isinstance(f_result, tuple):
                                        f_result = [f_result]
                                    if self._n == 1:
                                        f_result = [[_] for _ in f_result]
                                    for c_name, c_data in zip(c_own, f_result):
                                        if len(c_data) != self._n:
                                            raise ValueError('Data returned by apply function has length %i '
                                                             'but should be %i' % (len(c_data), self._n))
                                        f_results[c_name] = c_data
                                    f_result = None
                                    c_data = None
                        for _ in self._f_columns:
                            del kwargs[_]

                    for c in r.columns_name():
                        if c in self._apply_columns:
                            r[i, c] = f_results[c][(i + i_global) % self._n]
                    if 'pk' not in self._apply_columns:
                        if self._n > 1:
                            r[i, 'pk'] = result[0, 'pk']+str((i+i_global) % self._n)
                        else:
                            r[i, 'pk'] = result[0, 'pk']
                    # Clean
                    if (i+n+i_global) % self._n == 0:
                        f_keys = list(f_results.keys())
                        for f in f_keys:
                            del f_results[f]
                        del result
                        result = None
            else:
                # Batch wise (n=1)
                result = parent_gen.next(copy={c: r[c] for c in r.columns_name() if c not in self._apply_columns},
                                         limit=n, r=r)
                kwargs.update({_: result[_] for _ in self._f_columns})
                f_results = {}
                for c in r.columns_name():
                    if c in self._apply_columns:
                        if c not in f_results:
                            for c_own, c_parent in self._apply_col_dict.items():
                                if c not in c_own:
                                    continue
                                if len(c_parent) == 1:
                                    c_parent = c_parent[0]
                                f_result = match_params(self._f, batch=result[c_parent], column=c_parent, n=result.size,
                                                        **kwargs)
                                if len(c_own) == 1 and not isinstance(f_result, list) and not isinstance(f_result, tuple):
                                    f_results[list(c_own)[0]] = f_result
                                else:
                                    for c_name, c_data in zip(c_own, f_result):
                                        f_results[c_name] = c_data
                                f_result = None
                                c_data = None
                        r[c] = f_results[c]
                r['pk'] = result['pk']

                # Clean
                f_results = {}
                del result
                result = None
            r = None
            yield weakref

    @property
    def size(self):
        return self._parent.size * self._n

    def f_columns(self, columns):
        r = set(self._f_columns)
        for c in columns:
            if c != 'pk':
                r.update(self._columns_parent[c])
        return list(r)
