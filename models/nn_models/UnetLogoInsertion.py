import tensorflow as tf
import os
import numpy as np
import cv2
import yaml
import pandas as pd
import json
from scipy.signal import savgol_filter
from models.AbstractBannerReplacer import AbstractBannerReplacer


class UnetLogoInsertion(AbstractBannerReplacer):
    '''
    The model detects banner and replace it with other logo using Unet neural network model
    '''

    def __init__(self):
        self.model = None
        self.detected_mask = None
        self.detection_successful = False
        self.frame = None
        self.model_parameters = None
        self.corners = None
        self.old_width = None
        self.center_left = None
        self.center_right = None
        self.frame_num = 0
        self.before_smoothing = True
        self.load_smooth = True
        self.saved_points = pd.DataFrame(columns=['x_top_left', 'y_top_left', 'x_top_right',
                                                  'y_top_right', 'x_bot_left', 'y_bot_left',
                                                  'x_bot_right', 'y_bot_right'])

    def build_model(self, parameters_filepath):
        '''
        This method builds Unet neural network model and load trained weights
        :parameters_filepath: load model parameters from YAML file
        '''
        # loading and saving model parameters to class attribute
        with open(parameters_filepath, 'r') as file:
            self.model_parameters = yaml.safe_load(file)

        # load parameters
        img_height = self.model_parameters['img_height']
        img_width = self.model_parameters['img_width']
        img_channels = self.model_parameters['img_channels']
        model_weights_path = self.model_parameters['model_weights_path']
        train_model = self.model_parameters['train_model']

        # load json with model architecture
        with open("unet_model_architecture.json", "r") as read_model:
            model_json = json.load(read_model)

        # get model architecture from json
        self.model = tf.keras.models.model_from_json(model_json)

        # compile loaded model
        self.model.compile(optimizer='adam', loss=self.__loss, metrics=[self.__dice_coef])

        # training model if required
        if train_model:
            x_train_path = self.model_parameters['x_train_path']
            y_train_path = self.model_parameters['y_train_path']
            self.__train_model(x_train_path, y_train_path, img_height, img_width, img_channels, model_weights_path)

        # load trained model weights
        self.model.load_weights(model_weights_path)

    def detect_banner(self, frame):
        '''
        This method detects banner's pixels using Unet model, and saves deteсted binary mask
        and saves coordinates for top left and bottom right corners of a banner
        :frame: image or video frame where we will make detection and insertion
        '''
        self.frame = frame

        if self.before_smoothing:
            # load parameters
            value_threshold = self.model_parameters['value_threshold']

            # getting full size predicted mask of the frame
            fsz_mask = self.__predict_full_size()
            fsz_mask = (fsz_mask > value_threshold).astype(np.uint8)

            # check contours and detect corner points
            self.__check_contours(fsz_mask)

        else:
            # loading detected mask
            self.detected_mask = np.load('saved_frame_mask/frame{}.npy'.format(self.frame_num))

            # check if there is detected area on the frame
            if len(self.detected_mask) == 1:
                self.detection_successful = False

            else:
                # load smoothed points
                self.__load_points()
                self.detection_successful = True

        self.frame_num += 1

    def insert_logo(self):
        '''
        This method insert logo into detected area on the frame
        '''
        if not self.detection_successful:
            return

        # load logo
        logo = cv2.imread(self.model_parameters['logo_link'], cv2.IMREAD_UNCHANGED)

        # adjust logo color to banner's environment
        logo = self.__logo_color_adj(logo)

        # adjust logo to banner's shape
        transformed_logo = self.__adjust_logo_shape(logo)

        # replacing banner pixels with logo pixels
        for k in range(self.frame.shape[0]):
            for j in range(self.frame.shape[1]):
                if self.detected_mask[k, j] == 1:
                    self.frame[k, j] = transformed_logo[k, j]

    def __check_contours(self, fsz_mask):
        '''
        This method finding detected contours and corner coordinates
        :fsz_mask: detected full size mask
        '''
        # load parameters
        filter_area_size = self.model_parameters['filter_area_size']

        # finding contours
        first_cnt = True
        _, thresh = cv2.threshold(fsz_mask, 0.5, 255, 0)
        _, contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        for cnt in contours:
            if cv2.contourArea(cnt) > filter_area_size:

                # looking for coorner points
                rect = cv2.minAreaRect(cnt)
                box = cv2.boxPoints(rect)
                box = np.float32(box)

                # X and Y coordinates for center of detected rectangle
                xm, ym = rect[0]

                # detecting coordinates for each corner
                # works for the first contour
                if first_cnt:
                    first_cnt = False
                    for point in box:
                        if point[0] < xm:
                            if point[1] < ym:
                                top_left = point
                            else:
                                bot_left = point
                        else:
                            if point[1] < ym:
                                top_right = point
                            else:
                                bot_right = point

                    self.center_left = xm
                    self.center_right = xm

                # works with more than one contour, and replace coordinates with more relevant
                else:
                    # left side
                    if xm < self.center_left:
                        for point in box:
                            if point[0] < xm:
                                if point[1] < ym:
                                    top_left = point
                                else:
                                    bot_left = point
                        self.center_left = xm

                        # right side
                    elif xm > self.center_right:
                        for point in box:
                            if point[0] > xm:
                                if point[1] < ym:
                                    top_right = point
                                else:
                                    bot_right = point
                        self.center_right = xm

                # fill spaces in contours
                cv2.drawContours(fsz_mask, [cnt], -1, (1), -1)

        # return if there is no detected area
        if first_cnt:
            np.save('saved_frame_mask/frame{}.npy'.format(self.frame_num), np.zeros(1, dtype=np.uint8))
            return

        # saving detected mask
        np.save('saved_frame_mask/frame{}.npy'.format(self.frame_num), fsz_mask)

        # saving corner points to dataframe
        self.saved_points.loc[self.frame_num] = [top_left[0], top_left[1], top_right[0],
                                                 top_right[1], bot_left[0], bot_left[1],
                                                 bot_right[0], bot_right[1]]

    def __train_model(self, x_train_path, y_train_path, img_height, img_width, img_channels, model_weights_path):
        '''
        This method trains new model using X and Y train datasets
        :x_train_path: the path to X train dataset
        :y_train_path: the path to Y train dataset in .npy format
        :img_height: train image height
        :img_width: train image width
        :img_channels: number of channels for train image
        :model_weights_path: model weight path for saving
        '''
        # looking for files
        train_x_list = next(os.walk(x_train_path))[2]

        # empty lists for X_traind and Y_train
        x_train = np.zeros((len(train_x_list), img_height, img_width, img_channels), dtype=np.float32)
        y_train = np.zeros((len(train_x_list), img_height, img_width, 1), dtype=np.float32)

        # replacing empty lists elements with actual train data
        n = 0
        for file in train_x_list:
            x_train_file = x_train_path + file
            id_, _ = file.split('.')
            y_train_file = y_train_path + id_ + '.npy'
            x = cv2.imread(x_train_file, cv2.IMREAD_UNCHANGED)
            y = np.load(y_train_file)
            y_ = np.expand_dims(y, axis=-1)
            x_train[n] = x
            y_train[n] = y_
            n += 1

        # setting callbacks for the model
        callbacks = [tf.keras.callbacks.EarlyStopping(monitor='val_loss', patience=10, verbose=0),
                     tf.keras.callbacks.ModelCheckpoint(model_weights_path, monitor='val_loss', verbose=0,
                                                        save_best_only=True, save_weights_only=True)]

        # training the model
        self.model.fit(x_train, y_train, validation_split=0.1, epochs=200, callbacks=callbacks)

    def __loss(self, y_true, y_pred):
        '''
        Creating combined BCE and Dice Loss function which we will use in the model
        :y_true: actual Y values of test data
        :y_pred: predicted Y values of test data
        :return: combined BCE and Dice Loss function
        '''
        return tf.keras.losses.binary_crossentropy(y_true, y_pred) + self.__dice_loss(y_true, y_pred)

    def __dice_loss(self, y_true, y_pred):
        '''
        Creating a Dice Loss function for our model
        :y_true: actual Y values of test data
        :y_pred: predicted Y values of test data
        :return: Dice Loss function
        '''
        numerator = 2 * tf.reduce_sum(y_true * y_pred, axis=(1, 2, 3))
        denominator = tf.reduce_sum(y_true + y_pred, axis=(1, 2, 3))
        return tf.reshape(1 - numerator / denominator, (-1, 1, 1))

    def __dice_coef(self, y_true, y_pred):
        '''
        Creating a Dice Coefficient to use it like a metric in our model
        :y_true: actual Y values of test data
        :y_pred: predicted Y values of test data
        :return: Dice Coefficient
        '''
        numerator = 2 * tf.reduce_sum(y_true * y_pred, axis=-1)
        denominator = tf.reduce_sum(y_true + y_pred, axis=-1)
        return (numerator + 1) / (denominator + 1)

    def __adjust_logo_shape(self, logo):
        '''
        The method resizes and applies perspective transformation on logo
        :logo: the logo that we will transform
        :return: transformed logo
        '''

        # points before and after transformation
        pts1 = np.float32(
            [(0, 0), (0, (logo.shape[0] - 1)), ((logo.shape[1] - 1), (logo.shape[0] - 1)), ((logo.shape[1] - 1), 0)])
        pts2 = np.float32([self.corners[0], self.corners[3], self.corners[1], self.corners[2]])

        # crop frame when there is only part of it shown
        if round(self.corners[1][0]) >= (self.frame.shape[1] - 1) or round(self.corners[2][0]) >= (
                self.frame.shape[1] - 1):  # works for right side
            # calculated X point
            transform_x = self.corners[0][0] + self.old_width

            # correct Y points for cropped logo
            y_coef = self.old_width / (abs(self.corners[1][0] - self.corners[0][0]))
            transform_y_top = self.corners[0][1] + abs(self.corners[2][1] - self.corners[0][1]) * y_coef
            transform_y_bot = self.corners[3][1] + abs(self.corners[2][1] - self.corners[0][1]) * y_coef

            # calculated points
            pts2 = np.float32(
                [self.corners[0], self.corners[3], (transform_x, transform_y_bot), (transform_x, transform_y_top)])

        elif round(self.corners[0][0]) <= 0 or round(self.corners[3][0]) <= 0:  # works for left side
            # calculated X point
            transform_x = self.corners[2][0] - self.old_width

            # calculated points
            pts2 = np.float32([(transform_x, self.corners[0][1]), (transform_x, self.corners[3][1]), self.corners[1],
                               self.corners[2]])

        else:
            # saving real width of banner
            self.old_width = abs(self.corners[1][0] - self.corners[0][0])

        # perspective transformation
        mtrx = cv2.getPerspectiveTransform(pts1, pts2)
        transformed_logo = cv2.warpPerspective(logo, mtrx, (self.frame.shape[1], self.frame.shape[0]), borderMode=1)

        return transformed_logo

    def __logo_color_adj(self, logo):
        '''
        The method changes color of the logo to adjust it to frame
        :logo: the logo that we will change
        :return: changed logo
        '''
        # select banner area
        banner = self.frame[int(self.corners[0][1]):int(self.corners[1][1]),
                 int(self.corners[0][0]):int(self.corners[1][0])].copy()

        # get logo hsv
        logo_hsv = cv2.cvtColor(logo, cv2.COLOR_BGR2HSV)
        logo_h, logo_s, logo_v = cv2.split(logo_hsv)

        # get banner hsv
        banner_hsv = cv2.cvtColor(banner, cv2.COLOR_BGR2HSV)
        _, banner_s, _ = cv2.split(banner_hsv)

        # find the saturation difference between both images
        mean_logo_s = np.mean(logo_s).astype(int)
        mean_banner_s = np.mean(banner_s).astype(int)
        trans_coef = round(mean_banner_s / mean_logo_s, 2)

        # adjust logo saturation according to the difference
        adjusted_logo_s = (logo_s * trans_coef).astype('uint8')
        adjusted_logo_hsv = cv2.merge([logo_h, adjusted_logo_s, logo_v])
        adjusted_logo = cv2.cvtColor(adjusted_logo_hsv, cv2.COLOR_HSV2BGR)

        return adjusted_logo

    def __predict_full_size(self):
        '''
        The method goes trougth the frame and detects smaller areas,
        then combines them togeather
        :return: full size mask with detected banner pixels
        '''
        # load parameters
        img_height = self.model_parameters['img_height']
        img_width = self.model_parameters['img_width']
        step = self.model_parameters['full_size_step']

        # getting the frame size
        frame_height, frame_width, _ = self.frame.shape

        # create mask for full size image prediction
        fsz_mask = np.zeros((frame_height, frame_width, 1), dtype='float32')

        flag_k = False
        flag_j = False

        # split up the full frame to smaller images (same than using in model) and predict them
        for k in range(0, frame_height, step):
            if k + img_height >= (frame_height - 1):
                k = frame_height - img_height
                flag_k = True

            for j in range(0, frame_width, step):
                if j + img_width >= (frame_width - 1):
                    j = frame_width - img_width
                    flag_j = True

                mask_cr = fsz_mask[k:k + img_height, j:j + img_width]
                frame_cr = self.frame[k:k + img_height, j:j + img_width]
                test_cr = np.expand_dims(frame_cr, axis=0)
                cr_predict = self.model.predict(test_cr)

                for y in range(img_height):
                    for x in range(img_width):
                        if cr_predict[0][y, x] > mask_cr[y, x]:
                            mask_cr[y, x] = cr_predict[0][y, x]

                if flag_j:
                    break

            if flag_k:
                break

        return fsz_mask

    def __load_points(self):
        '''
        The method loads smoothed points
        '''
        # loading smoothed points for the 1st time and this is a video
        if self.load_smooth and self.model_parameters['source_type'] == 0:
            self.__smooth_points()
            self.load_smooth = False

        # getiing points
        top_left = (self.saved_points.loc[self.frame_num][0], self.saved_points.loc[self.frame_num][1])
        top_right = (self.saved_points.loc[self.frame_num][2], self.saved_points.loc[self.frame_num][3])
        bot_left = (self.saved_points.loc[self.frame_num][4], self.saved_points.loc[self.frame_num][5])
        bot_right = (self.saved_points.loc[self.frame_num][6], self.saved_points.loc[self.frame_num][7])

        # saving coordinates
        self.corners = [top_left, bot_right, top_right, bot_left]

    def __smooth_points(self):
        '''
        The method smoothes points
        '''

        def get_distance(corner_x, corner_y):
            return np.sqrt((self.saved_points['center_x'] - self.saved_points[corner_x]) ** 2 + (
                    self.saved_points['center_y'] - self.saved_points[corner_y]) ** 2)

        self.saved_points['center_x_1'] = (self.saved_points['x_top_left'] + self.saved_points['x_bot_right']) / 2
        self.saved_points['center_y_1'] = (self.saved_points['y_top_left'] + self.saved_points['y_bot_right']) / 2
        self.saved_points['center_x_2'] = (self.saved_points['x_top_right'] + self.saved_points['x_bot_left']) / 2
        self.saved_points['center_y_2'] = (self.saved_points['y_top_right'] + self.saved_points['y_bot_left']) / 2

        self.saved_points['center_x'] = (self.saved_points['center_x_1'] + self.saved_points['center_x_2']) / 2
        self.saved_points['center_y'] = (self.saved_points['center_y_1'] + self.saved_points['center_y_2']) / 2

        self.saved_points['dist_top_left'] = get_distance('x_top_left', 'y_top_left')
        self.saved_points['dist_bot_left'] = get_distance('x_bot_left', 'y_bot_left')
        self.saved_points['dist_top_right'] = get_distance('x_top_right', 'y_top_right')
        self.saved_points['dist_bot_right'] = get_distance('x_bot_right', 'y_bot_right')

        self.saved_points['left_height'] = np.sqrt(
            (self.saved_points['x_top_left'] - self.saved_points['x_bot_left']) ** 2 + (
                        self.saved_points['y_top_left'] - self.saved_points['y_bot_left']) ** 2)
        self.saved_points['top_width'] = abs(self.saved_points['x_top_left'] - self.saved_points['x_top_right'])
        self.saved_points['right_height'] = np.sqrt(
            (self.saved_points['x_top_right'] - self.saved_points['x_bot_right']) ** 2 + (
                        self.saved_points['y_top_right'] - self.saved_points['y_bot_right']) ** 2)
        self.saved_points['bot_width'] = abs(self.saved_points['x_bot_left'] - self.saved_points['x_bot_right'])

        self.saved_points['ratio'] = self.saved_points['top_width']/ self.saved_points['left_height']
        ratio = 6.6

        a = np.array(self.saved_points[['x_top_right', 'y_top_right']])
        b = np.array(self.saved_points[['x_bot_left', 'y_bot_left']])

        start_coordinate = np.array(self.saved_points[['x_top_left', 'y_top_left']])

        a = a - start_coordinate
        b = b - start_coordinate

        self.saved_points['cos_alpha'] = np.sum(a * b, axis=1) / (np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1))
        self.saved_points['angle'] = np.arccos(self.saved_points['cos_alpha']) * (180 / np.pi)

        self.saved_points.drop(columns=['center_x_1', 'center_y_1', 'center_x_2', 'center_y_2'], inplace=True)

        lost_side = []
        prev_x = self.saved_points.loc[0]
        for i in range(len(self.saved_points)):
            data = self.saved_points.loc[i]
            diff = data['dist_top_left'] - prev_x['dist_top_left']
            if abs(diff) > 5:
                lost_side.append(i)
            prev_x = data

        unstable_left = np.zeros(self.saved_points.shape[0])
        unstable_right = np.zeros_like(unstable_left)

        for i in lost_side:
            if abs(self.saved_points.loc[i, 'x_top_left'] - self.saved_points.loc[i - 1, 'x_top_left']) > 9:
                unstable_left[i] = 1

            if abs(self.saved_points.loc[i, 'x_top_right'] - self.saved_points.loc[i - 1, 'x_top_right']) > 9:
                unstable_right[i] = 1

        self.saved_points['unstable_right'] = unstable_right
        self.saved_points['unstable_left'] = unstable_left

        x_top_left = self.saved_points["x_top_left"]
        x_top_right = self.saved_points["x_top_right"]
        y_top_right = self.saved_points["y_top_right"]
        y_top_left = self.saved_points["y_top_left"]

        y = lambda x: (x - x_top_left) * (y_top_right - y_top_left) / (x_top_right - x_top_left) + y_top_left

        self.saved_points['y_top_right'] = y(self.saved_points['x_top_right'])
        self.saved_points['y_bot_right'] = y(self.saved_points['x_bot_right']) + self.saved_points["left_height"]

        latest_unstable = None
        for x in range(self.saved_points.shape[0]):
            row = self.saved_points.loc[x]
            x_top_left = row["x_top_left"]
            x_bot_left = row["x_bot_left"]
            x_top_right = row["x_top_right"]
            x_bot_right = row["x_bot_right"]
            y_top_right = row["y_top_right"]
            y_top_left = row["y_top_left"]

            if row['unstable_right']:
                latest_unstable = 'right'
                for position in range(x - 10, x + 10):
                    x_top_left = self.saved_points.loc[position, "x_top_left"]
                    x_bot_left = self.saved_points.loc[position, "x_bot_left"]

                    tmp_x_top_right = x_top_left + self.saved_points.loc[position, "left_height"] * \
                                      (ratio * self.saved_points.loc[position, "angle"] / 90)
                    tmp_x_bot_right = x_bot_left + self.saved_points.loc[position, "left_height"] * \
                                      (ratio * self.saved_points.loc[position, "angle"] / 90)

                    self.saved_points.loc[position, "x_top_right"] = tmp_x_top_right
                    self.saved_points.loc[position, "x_bot_right"] = tmp_x_bot_right

            if row['unstable_left']:
                latest_unstable = 'left'
                for position in range(x - 10, x + 10):
                    x_top_right = self.saved_points.loc[position, "x_top_right"]
                    x_bot_right = self.saved_points.loc[position, "x_bot_right"]

                    tmp_x_top_left = x_top_right - self.saved_points.loc[position, "right_height"] * \
                                     (ratio * self.saved_points.loc[position, "angle"] / 90)

                    tmp_x_bot_left = x_bot_right - self.saved_points.loc[position, "right_height"] * \
                                     (ratio * self.saved_points.loc[position, "angle"] / 90)

                    self.saved_points.loc[position, "x_top_left"] = tmp_x_top_left
                    self.saved_points.loc[position, "x_bot_left"] = tmp_x_bot_left

            if abs(row['ratio'] - ratio) > 0.05:
                if latest_unstable == 'left' and x_top_right <= 1278:
                    tmp_x_top_left = x_top_right - self.saved_points.loc[x, "right_height"] * \
                                     (ratio * self.saved_points.loc[x, "angle"] / 90)
                    tmp_x_bot_left = x_bot_right - self.saved_points.loc[x, "right_height"] * \
                                     (ratio * self.saved_points.loc[x, "angle"] / 90)

                    self.saved_points.loc[x, "x_top_left"] = tmp_x_top_left
                    self.saved_points.loc[x, "x_bot_left"] = tmp_x_bot_left

                if latest_unstable == 'right' and x_top_left >= 2:
                    tmp_x_top_right = x_top_left + self.saved_points.loc[x, "left_height"] * \
                                      (ratio * self.saved_points.loc[x, "angle"] / 90)
                    tmp_x_bot_right = x_bot_left + self.saved_points.loc[x, "left_height"] * \
                                      (ratio * self.saved_points.loc[x, "angle"] / 90)

                    self.saved_points.loc[x, "x_top_right"] = tmp_x_top_right
                    self.saved_points.loc[x, "x_bot_right"] = tmp_x_bot_right

        self.saved_points['y_top_left'] = self.__smooth_series(self.saved_points['y_top_left'])
        self.saved_points['y_top_right'] = self.__smooth_series(self.saved_points['y_top_right'])
        self.saved_points['y_bot_left'] = self.__smooth_series(self.saved_points['y_bot_left'])
        self.saved_points['y_bot_right'] = self.__smooth_series(self.saved_points['y_bot_right'])

    def __smooth_series(self, series):
        '''
        The method smoothes series of coordinates
        :series: series of coordinates to be smoothed
        :return: smoothed coordinates
        '''
        # load parameters
        min_window = self.model_parameters['min_window']
        max_window = self.model_parameters['max_window']
        poly_degree = self.model_parameters['poly_degree']
        threshold = self.model_parameters['smooth_threshold']

        best_series = []

        # smoothing
        for wnd_size in range(min_window, max_window):
            if wnd_size % 2 == 0:
                continue
            new_series = savgol_filter(series, wnd_size, poly_degree)
            if max(abs(new_series - series)) < threshold:
                best_series = new_series

        return best_series

